from __future__ import annotations

import asyncio
import hashlib
import re
import struct
import uuid
from dataclasses import dataclass
from typing import Protocol

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.service import artifact_service
from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import ObjectStorage
from backend.app.models import (
    Artifact,
    Blob,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabasePaperScope,
    DocumentPipelineVersion,
    DocumentReleaseEntry,
    PipelineDocument,
)

from .embeddings import document_embedding_service
from .executor import PipelineExecutor
from .indexing import document_index_service
from .service import DocumentDomainService, document_domain_service


@dataclass(frozen=True)
class PdfTextResult:
    pdf_sha256: str
    text: str


class PdfTextConverter(Protocol):
    async def convert(self, pdf: bytes) -> PdfTextResult: ...


class HttpPdfTextConverter:
    def __init__(
        self,
        client: AsyncClient,
        endpoint: str = "/api/v2/dev/fake-pipeline/pdf-to-text",
    ) -> None:
        self.client = client
        self.endpoint = endpoint

    async def convert(self, pdf: bytes) -> PdfTextResult:
        response = await self.client.post(
            self.endpoint,
            content=pdf,
            headers={"Content-Type": "application/pdf"},
        )
        response.raise_for_status()
        payload = response.json()
        expected_sha = hashlib.sha256(pdf).hexdigest()
        if payload.get("pdf_sha256") != expected_sha:
            raise RuntimeError("PDF-to-text service returned a mismatched fingerprint")
        return PdfTextResult(pdf_sha256=expected_sha, text=str(payload["text"]))


class DoublingPipelineExecutor(PipelineExecutor):
    """Fake LLM: output is source + blank line + source."""

    async def execute(
        self,
        *,
        messages: list[dict[str, str]],
        version: DocumentPipelineVersion,
    ) -> str:
        match = re.search(
            r'<source type="canonical_pdf_text">\n(.*?)\n</source>',
            messages[-1]["content"],
            flags=re.DOTALL,
        )
        if match is None:
            raise RuntimeError("Rendered Pipeline message has no canonical PDF text")
        source = match.group(1)
        return f"{source}\n\n{source}"


@dataclass(frozen=True)
class FakeIndexResult:
    kind: str
    release_id: uuid.UUID
    chunk_count: int
    manifest_sha256: str


class FakeIndexBuilder:
    def __init__(self, kind: str, *, latency_seconds: float = 60.0) -> None:
        clean_kind = kind.strip().upper()
        if clean_kind not in {"BM25", "VECTOR"}:
            raise ValueError("Fake index kind must be BM25 or VECTOR")
        if latency_seconds < 0:
            raise ValueError("Fake index latency cannot be negative")
        self.kind = clean_kind
        self.latency_seconds = latency_seconds

    async def build(
        self,
        release_id: uuid.UUID,
        chunk_manifest: tuple[tuple[uuid.UUID, str], ...],
    ) -> FakeIndexResult:
        await asyncio.sleep(self.latency_seconds)
        digest = hashlib.sha256()
        digest.update(self.kind.encode())
        digest.update(str(release_id).encode())
        for chunk_id, content_sha256 in chunk_manifest:
            digest.update(str(chunk_id).encode())
            digest.update(content_sha256.encode())
        return FakeIndexResult(
            kind=self.kind,
            release_id=release_id,
            chunk_count=len(chunk_manifest),
            manifest_sha256=digest.hexdigest(),
        )


class DeterministicEmbeddingClient:
    """Offline OpenAI-shaped test double with stable, non-zero vectors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: list[str], *, model: str, dimensions: int) -> list[list[float]]:
        self.calls.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            seed = hashlib.sha256(f"{model}\0{text}".encode()).digest()
            vector: list[float] = []
            for offset in range(dimensions):
                start = (offset * 4) % len(seed)
                block = (seed + seed)[start : start + 4]
                integer = struct.unpack(">I", block)[0]
                vector.append((integer / 2**32) * 2.0 - 1.0)
            vectors.append(vector)
        return vectors


@dataclass(frozen=True)
class FakeAcceptanceResult:
    outcome: str
    release_id: uuid.UUID | None
    converted_papers: int
    generated_documents: int
    reused_documents: int
    chunk_count: int
    indexes: tuple[FakeIndexResult, ...]


class FakePipelineAcceptanceCoordinator:
    """Executable acceptance path, not the future production job coordinator."""

    def __init__(self, documents: DocumentDomainService = document_domain_service) -> None:
        self.documents = documents

    async def run(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        database_id: uuid.UUID,
        pdf_converter: PdfTextConverter,
        pipeline_executor: PipelineExecutor,
        index_builders: tuple[FakeIndexBuilder, ...],
        build_mode: str = "UPDATE",
        trigger_reason: str = "FAKE_ACCEPTANCE",
    ) -> FakeAcceptanceResult:
        converted = await self._ensure_pdf_texts(
            session,
            storage,
            database_id=database_id,
            converter=pdf_converter,
        )
        release = await self.documents.start_reconcile(
            session,
            database_id,
            build_mode=build_mode,
            trigger_reason=trigger_reason,
        )
        if release is None:
            return FakeAcceptanceResult("NO_CHANGE", None, converted, 0, 0, 0, ())
        statuses = list(
            await session.scalars(
                select(DocumentReleaseEntry.status).where(
                    DocumentReleaseEntry.release_id == release.release_id
                )
            )
        )
        reused = sum(status == "REUSED" for status in statuses)
        generated = await self.documents.execute_release_inline(
            session,
            storage,
            pipeline_executor,
            release_id=release.release_id,
        )
        chunk_manifest = await self._chunk_manifest(session, release.release_id)
        indexes = tuple(
            await asyncio.gather(
                *(builder.build(release.release_id, chunk_manifest) for builder in index_builders)
            )
        )
        await document_index_service.build_release_index(session, release.release_id)
        if release.embedding_profile:
            await document_embedding_service.build_release_embeddings(
                session,
                storage,
                DeterministicEmbeddingClient(),
                release_id=release.release_id,
            )
        await self.documents.publish_release(session, release.release_id)
        return FakeAcceptanceResult(
            outcome="PUBLISHED",
            release_id=release.release_id,
            converted_papers=converted,
            generated_documents=generated,
            reused_documents=reused,
            chunk_count=len(chunk_manifest),
            indexes=indexes,
        )

    @staticmethod
    async def _ensure_pdf_texts(
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        database_id: uuid.UUID,
        converter: PdfTextConverter,
    ) -> int:
        database = await session.get(DocumentDatabase, database_id)
        if database is None:
            raise LookupError("Document Database not found")
        if database.range_mode != "EXPLICIT":
            raise NotImplementedError("Fake acceptance supports EXPLICIT ranges only")
        paper_ids = list(
            await session.scalars(
                select(DocumentDatabasePaperScope.canonical_paper_id)
                .where(DocumentDatabasePaperScope.database_id == database_id)
                .order_by(DocumentDatabasePaperScope.canonical_paper_id)
            )
        )
        converted = 0
        for paper_id in paper_ids:
            pdf_artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.canonical_paper_id == paper_id,
                    Artifact.artifact_type == "SOURCE_PDF",
                    Artifact.status == "ACTIVE",
                )
            )
            if pdf_artifact is None:
                raise RuntimeError(f"Canonical PDF is missing for Paper {paper_id}")
            pdf_blob = await session.get(Blob, pdf_artifact.blob_id)
            if pdf_blob is None or pdf_blob.status != "AVAILABLE":
                raise RuntimeError(f"Canonical PDF Blob is unavailable for Paper {paper_id}")
            existing_text = await session.scalar(
                select(Artifact).where(
                    Artifact.canonical_paper_id == paper_id,
                    Artifact.artifact_type == "EXTRACTED_TEXT",
                    Artifact.status == "ACTIVE",
                    Artifact.source_fingerprint == pdf_blob.sha256,
                )
            )
            if existing_text is not None:
                continue
            pdf = await storage.read_bytes(pdf_blob.storage_key, pdf_blob.byte_size + 1)
            result = await converter.convert(pdf)
            if result.pdf_sha256 != pdf_blob.sha256:
                raise RuntimeError("PDF-to-text result does not match the canonical PDF")
            text_blob = await blob_service.store_bytes(
                session,
                storage,
                data=result.text.encode("utf-8"),
                media_type="text/plain; charset=utf-8",
                actor_principal_id=None,
            )
            await artifact_service.set_canonical(
                session,
                canonical_paper_id=paper_id,
                artifact_key="pdf-text",
                artifact_type="EXTRACTED_TEXT",
                blob_id=text_blob.blob_id,
                media_type="text/plain; charset=utf-8",
                actor_principal_id=None,
                original_filename="pdf-text.txt",
                provenance={
                    "converter": "fake-rest-pdf-to-text",
                    "source_pdf_blob_id": str(pdf_blob.blob_id),
                },
                source_fingerprint=pdf_blob.sha256,
            )
            converted += 1
        return converted

    @staticmethod
    async def _chunk_manifest(
        session: AsyncSession, release_id: uuid.UUID
    ) -> tuple[tuple[uuid.UUID, str], ...]:
        rows = (
            await session.execute(
                select(DocumentChunk.chunk_id, DocumentChunk.content_sha256)
                .join(
                    PipelineDocument,
                    PipelineDocument.document_id == DocumentChunk.document_id,
                )
                .join(
                    DocumentReleaseEntry,
                    DocumentReleaseEntry.document_id == PipelineDocument.document_id,
                )
                .where(DocumentReleaseEntry.release_id == release_id)
                .order_by(DocumentChunk.chunk_id)
            )
        ).all()
        return tuple((chunk_id, content_sha256) for chunk_id, content_sha256 in rows)


fake_pipeline_acceptance_coordinator = FakePipelineAcceptanceCoordinator()
