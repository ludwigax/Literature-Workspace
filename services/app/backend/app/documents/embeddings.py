from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import faiss
import httpx
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import ObjectStorage
from backend.app.config import Settings, get_settings
from backend.app.models import (
    Blob,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabaseRelease,
    DocumentIndexFacetBitmap,
    DocumentIndexManifestRow,
    DocumentReleaseIndex,
)

from .indexing import _hash_json


class EmbeddingClient(Protocol):
    async def embed(
        self, texts: list[str], *, model: str, dimensions: int
    ) -> list[list[float]]: ...


class OpenAICompatibleEmbeddingClient:
    """Small OpenAI-compatible embeddings client with bounded network retries."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        self.client = client
        clean_url = base_url.rstrip("/")
        self.endpoint = (
            clean_url if clean_url.endswith("/embeddings") else f"{clean_url}/embeddings"
        )
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def embed(self, texts: list[str], *, model: str, dimensions: int) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.post(
                    self.endpoint,
                    headers=headers,
                    json={
                        "model": model,
                        "input": texts,
                        "dimensions": dimensions,
                        "encoding_format": "float",
                    },
                    timeout=self.timeout_seconds,
                )
                if (response.status_code == 413 or response.status_code >= 500) and len(texts) > 1:
                    middle = len(texts) // 2
                    left = await self.embed(texts[:middle], model=model, dimensions=dimensions)
                    right = await self.embed(texts[middle:], model=model, dimensions=dimensions)
                    return [*left, *right]
                response.raise_for_status()
                return self._parse(response.json(), len(texts), dimensions)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(min(2**attempt, 4))
        raise AssertionError("unreachable")

    @staticmethod
    def _parse(payload: Any, count: int, dimensions: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError("Embedding response has no data array")
        vectors: list[list[float] | None] = [None] * count
        for value in payload["data"]:
            if not isinstance(value, dict) or not isinstance(value.get("index"), int):
                raise RuntimeError("Embedding response item has no integer index")
            index = value["index"]
            raw = value.get("embedding")
            if index < 0 or index >= count or vectors[index] is not None:
                raise RuntimeError("Embedding response contains an invalid or duplicate index")
            if not isinstance(raw, list) or len(raw) != dimensions:
                raise RuntimeError("Embedding response has an unexpected vector dimension")
            vector = [float(number) for number in raw]
            if not all(math.isfinite(number) for number in vector):
                raise RuntimeError("Embedding response contains a non-finite value")
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise RuntimeError("Embedding response omitted one or more inputs")
        return [vector for vector in vectors if vector is not None]


@dataclass(frozen=True)
class EmbeddingProfile:
    model: str
    dimensions: int
    batch_size: int
    max_batch_tokens: int

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dimensions": self.dimensions,
            "metric": "COSINE",
            "normalization": "L2",
            "index_type": "FLAT_IP",
        }

    @property
    def profile_hash(self) -> str:
        return _hash_json(self.identity)


@dataclass(frozen=True)
class EmbeddingSearchHit:
    chunk_id: uuid.UUID
    row_number: int
    score: float


def resolve_embedding_profile(
    value: dict[str, Any], settings: Settings | None = None
) -> EmbeddingProfile:
    settings = settings or get_settings()
    model = str(value.get("model") or settings.embedding_model).strip()
    dimensions = int(value.get("dimensions") or settings.embedding_dimensions)
    batch_size = int(value.get("batch_size") or settings.embedding_batch_size)
    max_batch_tokens = int(value.get("max_batch_tokens") or settings.embedding_max_batch_tokens)
    if not model:
        raise ValueError("Embedding model cannot be empty")
    if dimensions < 1 or dimensions > 65_536:
        raise ValueError("Embedding dimensions are outside the supported range")
    if batch_size < 1 or batch_size > 2048:
        raise ValueError("Embedding batch size is outside the supported range")
    if max_batch_tokens < 1 or max_batch_tokens > 1_000_000:
        raise ValueError("Embedding batch token budget is outside the supported range")
    return EmbeddingProfile(
        model=model,
        dimensions=dimensions,
        batch_size=batch_size,
        max_batch_tokens=max_batch_tokens,
    )


def estimate_embedding_tokens(text: str) -> int:
    """Conservatively estimate multilingual tokens without a model tokenizer."""
    cjk = 0
    other = 0
    for character in text:
        if character.isspace():
            continue
        codepoint = ord(character)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            cjk += 1
        else:
            other += 1
    return max(1, 8 + cjk + math.ceil(other / 4))


def embedding_batches(
    positions: list[int],
    texts: list[str],
    *,
    max_items: int,
    max_tokens: int,
) -> list[list[int]]:
    """Pack ordered row positions under item and estimated-token ceilings."""
    batches: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0
    for position in positions:
        tokens = estimate_embedding_tokens(texts[position])
        if current and (len(current) >= max_items or current_tokens + tokens > max_tokens):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(position)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


class DocumentEmbeddingService:
    """Build and query the FAISS index owned by one Document Release."""

    async def build_release_embeddings(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        client: EmbeddingClient,
        *,
        release_id: uuid.UUID,
        settings: Settings | None = None,
    ) -> DocumentReleaseIndex:
        release = await session.get(DocumentDatabaseRelease, release_id, with_for_update=True)
        if release is None:
            raise LookupError("Document Database Release not found")
        if release.status != "BUILDING":
            raise RuntimeError("Only a BUILDING Release can build embeddings")
        if not release.embedding_profile:
            raise RuntimeError("Release has no embedding profile")
        release_index = await session.get(DocumentReleaseIndex, release_id, with_for_update=True)
        if release_index is None or release_index.bm25_status != "READY":
            raise RuntimeError("Build the Release manifest before embeddings")
        profile = resolve_embedding_profile(dict(release.embedding_profile), settings)
        if (
            release_index.embedding_status == "READY"
            and release_index.embedding_profile_hash == profile.profile_hash
            and release_index.embedding_index_blob_id is not None
        ):
            return release_index

        release_index.embedding_status = "BUILDING"
        release_index.embedding_profile_hash = profile.profile_hash
        release_index.embedding_model = profile.model
        release_index.embedding_dimensions = profile.dimensions
        release_index.embedding_metric = "COSINE"
        release_index.embedding_index_type = "FLAT_IP"
        release.retrieval_status = "PENDING"
        await session.flush()
        try:
            result = await session.execute(
                select(DocumentIndexManifestRow, DocumentChunk.content)
                .join(
                    DocumentChunk,
                    DocumentChunk.chunk_id == DocumentIndexManifestRow.chunk_id,
                )
                .where(DocumentIndexManifestRow.release_id == release_id)
                .order_by(DocumentIndexManifestRow.row_number)
            )
            rows = [(manifest, content) for manifest, content in result]
            if len(rows) != release_index.row_count:
                raise RuntimeError("Embedding manifest is incomplete")
            matrix = np.empty((len(rows), profile.dimensions), dtype=np.float32)
            filled = np.zeros(len(rows), dtype=np.bool_)
            await self._reuse_current_vectors(
                session,
                storage,
                release,
                profile,
                rows,
                matrix,
                filled,
            )
            missing = [number for number, value in enumerate(filled) if not value]
            texts = [content for _, content in rows]
            for positions in embedding_batches(
                missing,
                texts,
                max_items=profile.batch_size,
                max_tokens=profile.max_batch_tokens,
            ):
                vectors = await client.embed(
                    [texts[position] for position in positions],
                    model=profile.model,
                    dimensions=profile.dimensions,
                )
                if len(vectors) != len(positions):
                    raise RuntimeError("Embedding client returned an unexpected vector count")
                matrix[np.asarray(positions, dtype=np.int64)] = np.asarray(
                    vectors, dtype=np.float32
                )
                filled[np.asarray(positions, dtype=np.int64)] = True
            if not bool(np.all(filled)):
                raise RuntimeError("Embedding matrix contains unfilled rows")
            if len(rows):
                norms = np.linalg.norm(matrix, axis=1)
                if not bool(np.all(np.isfinite(norms))) or bool(np.any(norms == 0)):
                    raise RuntimeError("Embedding matrix contains invalid or zero vectors")
                faiss.normalize_L2(matrix)
            faiss_index = faiss.IndexFlatIP(profile.dimensions)
            faiss_index.add(matrix)
            serialized = faiss.serialize_index(faiss_index).tobytes()
            blob = await blob_service.store_bytes(
                session,
                storage,
                data=serialized,
                media_type="application/x-faiss-index",
                actor_principal_id=None,
            )
            release_index.embedding_index_blob_id = blob.blob_id
            release_index.embedding_index_sha256 = blob.sha256
            release_index.embedding_status = "READY"
            release.retrieval_status = "READY"
            await session.flush()
            return release_index
        except Exception:
            release_index.embedding_status = "FAILED"
            release.retrieval_status = "FAILED"
            await session.flush()
            raise

    async def search_release(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        release_id: uuid.UUID,
        query_vector: list[float],
        limit: int = 10,
        facet_1: str | None = None,
        facet_2: str | None = None,
    ) -> list[EmbeddingSearchHit]:
        if limit < 1:
            return []
        release_index = await session.get(DocumentReleaseIndex, release_id)
        if (
            release_index is None
            or release_index.embedding_status != "READY"
            or release_index.embedding_index_blob_id is None
            or release_index.embedding_dimensions is None
        ):
            raise LookupError("Release embedding index is not ready")
        vector = np.asarray([query_vector], dtype=np.float32)
        if vector.shape != (1, release_index.embedding_dimensions):
            raise ValueError("Query vector dimension does not match the Release index")
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("Query vector must be finite and non-zero")
        faiss.normalize_L2(vector)
        index = await self._load_index(session, storage, release_index)
        params = None
        bitmap_buffer = None
        selector = None
        if facet_1 is not None or facet_2 is not None:
            bitmap = await self._combined_bitmap(
                session,
                release_id,
                release_index.row_count,
                facet_1=facet_1,
                facet_2=facet_2,
            )
            bitmap_buffer = np.frombuffer(bitmap, dtype=np.uint8).copy()
            selector = faiss.IDSelectorBitmap(
                release_index.row_count, faiss.swig_ptr(bitmap_buffer)
            )
            params = faiss.SearchParameters()
            params.sel = selector
        scores, labels = index.search(vector, limit, params=params)
        valid = [int(row) for row in labels[0] if row >= 0]
        mappings = {
            row.row_number: row.chunk_id
            for row in await session.scalars(
                select(DocumentIndexManifestRow).where(
                    DocumentIndexManifestRow.release_id == release_id,
                    DocumentIndexManifestRow.row_number.in_(valid),
                )
            )
        }
        return [
            EmbeddingSearchHit(chunk_id=mappings[int(row)], row_number=int(row), score=float(score))
            for score, row in zip(scores[0], labels[0], strict=True)
            if row >= 0 and int(row) in mappings
        ]

    async def _reuse_current_vectors(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        release: DocumentDatabaseRelease,
        profile: EmbeddingProfile,
        rows: list[tuple[DocumentIndexManifestRow, str]],
        matrix: np.ndarray,
        filled: np.ndarray,
    ) -> None:
        database = await session.get(DocumentDatabase, release.database_id)
        if database is None or database.current_release_id is None:
            return
        current_index = await session.get(DocumentReleaseIndex, database.current_release_id)
        if (
            current_index is None
            or current_index.embedding_status != "READY"
            or current_index.embedding_profile_hash != profile.profile_hash
            or current_index.embedding_dimensions != profile.dimensions
            or current_index.embedding_index_blob_id is None
        ):
            return
        current_rows = {
            row.chunk_id: row
            for row in await session.scalars(
                select(DocumentIndexManifestRow).where(
                    DocumentIndexManifestRow.release_id == database.current_release_id
                )
            )
        }
        reusable: list[tuple[int, int]] = []
        for destination, (row, _) in enumerate(rows):
            old = current_rows.get(row.chunk_id)
            if old is not None and old.content_sha256 == row.content_sha256:
                reusable.append((destination, old.row_number))
        if not reusable:
            return
        old_faiss = await self._load_index(session, storage, current_index)
        old_numbers = np.asarray([source for _, source in reusable], dtype=np.int64)
        vectors = old_faiss.reconstruct_batch(old_numbers)
        for (destination, _), vector in zip(reusable, vectors, strict=True):
            matrix[destination] = vector
            filled[destination] = True

    @staticmethod
    async def _load_index(
        session: AsyncSession,
        storage: ObjectStorage,
        release_index: DocumentReleaseIndex,
    ) -> faiss.Index:
        blob = await session.get(Blob, release_index.embedding_index_blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise RuntimeError("FAISS Blob is unavailable")
        data = await storage.read_bytes(blob.storage_key, blob.byte_size + 1)
        digest = hashlib.sha256(data).hexdigest()
        if digest != blob.sha256 or digest != release_index.embedding_index_sha256:
            raise RuntimeError("FAISS Blob failed SHA-256 verification")
        index = faiss.deserialize_index(np.frombuffer(data, dtype=np.uint8))
        if index.ntotal != release_index.row_count or index.d != release_index.embedding_dimensions:
            raise RuntimeError("FAISS index metadata does not match its Release")
        return index

    @staticmethod
    async def _combined_bitmap(
        session: AsyncSession,
        release_id: uuid.UUID,
        row_count: int,
        *,
        facet_1: str | None,
        facet_2: str | None,
    ) -> bytes:
        size = (row_count + 7) // 8
        combined = bytearray([255] * size)
        if size and row_count % 8:
            combined[-1] &= (1 << (row_count % 8)) - 1
        for slot, value in ((1, facet_1), (2, facet_2)):
            if value is None:
                continue
            entry = await session.get(
                DocumentIndexFacetBitmap,
                {"release_id": release_id, "facet_slot": slot, "facet_value": value},
            )
            if entry is None:
                return bytes(size)
            for offset, item in enumerate(entry.bitmap):
                combined[offset] &= item
        return bytes(combined)


def openai_embedding_client(
    client: httpx.AsyncClient, settings: Settings | None = None
) -> OpenAICompatibleEmbeddingClient:
    settings = settings or get_settings()
    api_key = (
        settings.embedding_api_key.get_secret_value()
        if settings.embedding_api_key is not None
        else None
    )
    return OpenAICompatibleEmbeddingClient(
        client,
        base_url=settings.embedding_base_url,
        api_key=api_key,
        timeout_seconds=settings.embedding_timeout_seconds,
        max_retries=settings.embedding_max_retries,
    )


document_embedding_service = DocumentEmbeddingService()
