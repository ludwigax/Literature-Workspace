from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabaseRelease,
    DocumentIndexFacetBitmap,
    DocumentIndexManifestRow,
    DocumentReleaseEntry,
    DocumentReleaseIndex,
)

_TOKEN_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _analyzer_config(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "lowercase": bool(profile.get("lowercase", True)),
        "min_token_length": max(1, int(profile.get("min_token_length", 1))),
        "stopwords": sorted(
            {str(value).casefold() for value in profile.get("stopwords", []) if str(value).strip()}
        ),
    }


def _term_frequencies(text: str, config: dict[str, Any]) -> tuple[int, dict[str, int]]:
    stopwords = set(config["stopwords"])
    minimum = int(config["min_token_length"])
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if config["lowercase"]:
            token = token.casefold()
        if len(token) >= minimum and token.casefold() not in stopwords:
            tokens.append(token)
    return len(tokens), dict(Counter(tokens))


def _bitmap(rows: list[int], row_count: int) -> bytes:
    value = bytearray((row_count + 7) // 8)
    for row in rows:
        value[row // 8] |= 1 << (row % 8)
    return bytes(value)


def bitmap_rows(value: bytes, row_count: int) -> set[int]:
    return {row for row in range(row_count) if value[row // 8] & (1 << (row % 8))}


@dataclass(frozen=True)
class _ChunkSnapshot:
    chunk_id: uuid.UUID
    content: str
    content_sha256: str
    facet_1: str | None
    facet_2: str | None


class DocumentIndexService:
    """Build disposable Release-local manifests and retrieval caches."""

    async def build_release_index(
        self, session: AsyncSession, release_id: uuid.UUID
    ) -> DocumentReleaseIndex:
        release = await session.get(DocumentDatabaseRelease, release_id, with_for_update=True)
        if release is None:
            raise LookupError("Document Database Release not found")
        if release.status != "BUILDING":
            raise RuntimeError("Only a BUILDING Release can build an index")
        database = await session.get(DocumentDatabase, release.database_id)
        if database is None:
            raise RuntimeError("Document Database is missing")

        chunks = await self._release_chunks(session, release_id)
        current_rows = await self._current_rows(session, database.current_release_id)
        row_by_chunk = self._assign_rows(chunks, current_rows)
        ordered = sorted(chunks, key=lambda chunk: row_by_chunk[chunk.chunk_id])

        analyzer = _analyzer_config(dict(release.bm25_profile))
        analyzer_hash = _hash_json(analyzer)
        reusable = {
            row.chunk_id: row for row in current_rows if row.bm25_analyzer_hash == analyzer_hash
        }

        # Rebuilding one BUILDING Release is idempotent. The CURRENT Release is never touched.
        await session.execute(
            delete(DocumentReleaseIndex).where(DocumentReleaseIndex.release_id == release_id)
        )
        await session.flush()

        manifest_payload: list[dict[str, Any]] = []
        manifest_rows: list[DocumentIndexManifestRow] = []
        facet_rows: dict[tuple[int, str], list[int]] = defaultdict(list)
        document_frequencies: Counter[str] = Counter()
        total_document_length = 0
        for chunk in ordered:
            row_number = row_by_chunk[chunk.chunk_id]
            old = reusable.get(chunk.chunk_id)
            if old is not None and old.content_sha256 == chunk.content_sha256:
                document_length = old.bm25_document_length
                frequencies = dict(old.bm25_term_frequencies)
            else:
                document_length, frequencies = _term_frequencies(chunk.content, analyzer)
            total_document_length += document_length
            document_frequencies.update(frequencies.keys())
            manifest_payload.append(
                {
                    "row": row_number,
                    "chunk_id": str(chunk.chunk_id),
                    "content_sha256": chunk.content_sha256,
                    "facet_1": chunk.facet_1,
                    "facet_2": chunk.facet_2,
                }
            )
            manifest_rows.append(
                DocumentIndexManifestRow(
                    release_id=release_id,
                    row_number=row_number,
                    chunk_id=chunk.chunk_id,
                    content_sha256=chunk.content_sha256,
                    facet_1=chunk.facet_1,
                    facet_2=chunk.facet_2,
                    bm25_analyzer_hash=analyzer_hash,
                    bm25_document_length=document_length,
                    bm25_term_frequencies=frequencies,
                )
            )
            if chunk.facet_1 is not None:
                facet_rows[(1, chunk.facet_1)].append(row_number)
            if chunk.facet_2 is not None:
                facet_rows[(2, chunk.facet_2)].append(row_number)

        count = len(ordered)
        inverse_document_frequencies = {
            term: math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequencies.items()
        }
        index = DocumentReleaseIndex(
            release_id=release_id,
            status="READY",
            manifest_hash=_hash_json(manifest_payload),
            row_count=count,
            bm25_status="READY",
            bm25_analyzer_hash=analyzer_hash,
            bm25_document_count=count,
            bm25_average_document_length=(total_document_length / count if count else 0.0),
            bm25_document_frequencies=dict(document_frequencies),
            bm25_inverse_document_frequencies=inverse_document_frequencies,
            embedding_status="BUILDING" if release.embedding_profile else "NOT_CONFIGURED",
            embedding_profile_hash=(
                _hash_json(dict(release.embedding_profile)) if release.embedding_profile else None
            ),
        )
        session.add(index)
        # These models intentionally have no ORM relationships; flush the
        # owning row before inserting its FK children.
        await session.flush()
        session.add_all(manifest_rows)
        session.add_all(
            DocumentIndexFacetBitmap(
                release_id=release_id,
                facet_slot=slot,
                facet_value=value,
                row_count=count,
                bitmap=_bitmap(rows, count),
            )
            for (slot, value), rows in facet_rows.items()
        )
        if release.retrieval_status != "READY":
            release.retrieval_status = "PENDING" if release.embedding_profile else "READY"
        await session.flush()
        return index

    async def facet_filter_rows(
        self,
        session: AsyncSession,
        release_id: uuid.UUID,
        *,
        facet_1: str | None = None,
        facet_2: str | None = None,
    ) -> set[int]:
        index = await session.get(DocumentReleaseIndex, release_id)
        if index is None or index.status != "READY":
            raise LookupError("Release index is not ready")
        selected = set(range(index.row_count))
        for slot, value in ((1, facet_1), (2, facet_2)):
            if value is None:
                continue
            entry = await session.get(
                DocumentIndexFacetBitmap,
                {"release_id": release_id, "facet_slot": slot, "facet_value": value},
            )
            if entry is None:
                return set()
            selected &= bitmap_rows(entry.bitmap, entry.row_count)
        return selected

    @staticmethod
    async def discard_release_index(session: AsyncSession, release_id: uuid.UUID) -> None:
        await session.execute(
            delete(DocumentReleaseIndex).where(DocumentReleaseIndex.release_id == release_id)
        )
        await session.flush()

    @staticmethod
    async def _release_chunks(session: AsyncSession, release_id: uuid.UUID) -> list[_ChunkSnapshot]:
        rows = (
            await session.execute(
                select(DocumentChunk)
                .join(
                    DocumentReleaseEntry,
                    DocumentReleaseEntry.document_id == DocumentChunk.document_id,
                )
                .where(DocumentReleaseEntry.release_id == release_id)
                .order_by(
                    DocumentReleaseEntry.canonical_paper_id,
                    DocumentChunk.document_id,
                    DocumentChunk.ordinal,
                    DocumentChunk.chunk_id,
                )
            )
        ).scalars()
        return [
            _ChunkSnapshot(
                chunk_id=chunk.chunk_id,
                content=chunk.content,
                content_sha256=chunk.content_sha256,
                facet_1=chunk.facet_1,
                facet_2=chunk.facet_2,
            )
            for chunk in rows
        ]

    @staticmethod
    async def _current_rows(
        session: AsyncSession, current_release_id: uuid.UUID | None
    ) -> list[DocumentIndexManifestRow]:
        if current_release_id is None:
            return []
        return list(
            await session.scalars(
                select(DocumentIndexManifestRow)
                .where(DocumentIndexManifestRow.release_id == current_release_id)
                .order_by(DocumentIndexManifestRow.row_number)
            )
        )

    @staticmethod
    def _assign_rows(
        chunks: list[_ChunkSnapshot], current_rows: list[DocumentIndexManifestRow]
    ) -> dict[uuid.UUID, int]:
        desired = {chunk.chunk_id for chunk in chunks}
        count = len(desired)
        assigned: dict[uuid.UUID, int] = {}
        occupied: set[int] = set()
        deferred: list[uuid.UUID] = []
        old_ids: set[uuid.UUID] = set()
        for row in current_rows:
            if row.chunk_id not in desired:
                continue
            old_ids.add(row.chunk_id)
            if row.row_number < count and row.row_number not in occupied:
                assigned[row.chunk_id] = row.row_number
                occupied.add(row.row_number)
            else:
                deferred.append(row.chunk_id)
        new_ids = sorted(desired - old_ids, key=str)
        waiting = iter([*new_ids, *deferred])
        for row_number in range(count):
            if row_number not in occupied:
                assigned[next(waiting)] = row_number
        return assigned


document_index_service = DocumentIndexService()
