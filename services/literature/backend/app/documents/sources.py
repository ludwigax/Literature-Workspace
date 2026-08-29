from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.service import artifact_service
from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import ObjectStorage
from backend.app.models import Artifact, Blob


@dataclass(frozen=True)
class PdfTextSource:
    canonical_paper_id: uuid.UUID
    source_artifact_id: uuid.UUID
    filename: str
    pdf_sha256: str
    pdf: bytes


@dataclass(frozen=True)
class PdfTextResult:
    filename: str
    pdf_sha256: str
    text: str


class PdfTextConverter(Protocol):
    async def submit(self, sources: list[PdfTextSource]) -> str: ...

    async def wait_and_fetch(
        self, job_id: str, sources: list[PdfTextSource]
    ) -> list[PdfTextResult]: ...


class PdfTextRemoteJobError(RuntimeError):
    """The remote job reached its terminal error state and may be resubmitted."""


class HttpPdfTextConverter:
    """Client for the asynchronous MinerU batch extraction service."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        request_timeout_seconds: float = 60,
        job_timeout_seconds: float = 3600,
        poll_interval_seconds: float = 2,
    ) -> None:
        clean = endpoint.rstrip("/")
        self.base_url = clean[: -len("/extract")] if clean.endswith("/extract") else clean
        self.client = client
        self.request_timeout_seconds = request_timeout_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    async def submit(self, sources: list[PdfTextSource]) -> str:
        if not sources:
            raise ValueError("PDF-to-text batch cannot be empty")
        response = await self.client.post(
            f"{self.base_url}/extract",
            files=[
                ("files", (source.filename, source.pdf, "application/pdf")) for source in sources
            ],
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = self._mapping(response.json(), "PDF-to-text submission")
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise RuntimeError("PDF-to-text submission returned no job_id")
        return job_id.strip()

    async def wait_and_fetch(
        self, job_id: str, sources: list[PdfTextSource]
    ) -> list[PdfTextResult]:
        deadline = time.monotonic() + self.job_timeout_seconds
        while True:
            response = await self.client.get(
                f"{self.base_url}/job/{job_id}", timeout=self.request_timeout_seconds
            )
            response.raise_for_status()
            payload = self._mapping(response.json(), "PDF-to-text job status")
            status = str(payload.get("status") or "").strip().lower()
            if status == "done":
                break
            if status == "error":
                detail = payload.get("error") or payload.get("detail") or "unknown error"
                raise PdfTextRemoteJobError(f"PDF-to-text job failed: {detail}")
            if status not in {"queued", "processing"}:
                raise RuntimeError(
                    f"PDF-to-text job returned unknown status: {status or '<empty>'}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"PDF-to-text job {job_id} exceeded its polling deadline")
            await asyncio.sleep(self.poll_interval_seconds)

        response = await self.client.get(
            f"{self.base_url}/job/{job_id}/markdown",
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = self._mapping(response.json(), "PDF-to-text result")
        markdown = payload.get("markdown")
        if not isinstance(markdown, dict):
            raise RuntimeError("PDF-to-text result has no markdown filename map")
        results: list[PdfTextResult] = []
        for source in sources:
            text = markdown.get(source.filename)
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError(
                    f"PDF-to-text result omitted non-empty markdown for {source.filename}"
                )
            results.append(
                PdfTextResult(
                    filename=source.filename,
                    pdf_sha256=source.pdf_sha256,
                    text=text,
                )
            )
        return results

    @staticmethod
    def _mapping(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} returned a non-object response")
        return value


class CanonicalPdfTextService:
    async def prepare_batch(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        entries: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> list[PdfTextSource]:
        sources: list[PdfTextSource] = []
        for canonical_paper_id, source_artifact_id in entries:
            artifact = await session.get(Artifact, source_artifact_id)
            if (
                artifact is None
                or artifact.canonical_paper_id != canonical_paper_id
                or artifact.artifact_type != "SOURCE_PDF"
                or artifact.status != "ACTIVE"
            ):
                raise RuntimeError("Canonical source PDF is unavailable")
            blob = await session.get(Blob, artifact.blob_id)
            if blob is None or blob.status != "AVAILABLE":
                raise RuntimeError("Canonical source PDF Blob is unavailable")
            pdf = await storage.read_bytes(blob.storage_key, blob.byte_size + 1)
            if hashlib.sha256(pdf).hexdigest() != blob.sha256:
                raise RuntimeError("Canonical source PDF failed SHA-256 verification")
            sources.append(
                PdfTextSource(
                    canonical_paper_id=canonical_paper_id,
                    source_artifact_id=source_artifact_id,
                    filename=f"{canonical_paper_id}.pdf",
                    pdf_sha256=blob.sha256,
                    pdf=pdf,
                )
            )
        return sources

    async def persist_batch(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        sources: list[PdfTextSource],
        results: list[PdfTextResult],
        *,
        actor_principal_id: uuid.UUID | None = None,
    ) -> list[Artifact]:
        if len(results) != len(sources):
            raise RuntimeError("PDF-to-text result count does not match its batch")
        artifacts: list[Artifact] = []
        for source, result in zip(sources, results, strict=True):
            if result.filename != source.filename or result.pdf_sha256 != source.pdf_sha256:
                raise RuntimeError("PDF-to-text result does not match its source PDF")
            if not result.text.strip():
                raise RuntimeError("PDF-to-text result is empty")
            current = await session.get(Artifact, source.source_artifact_id)
            if current is None:
                raise RuntimeError("Canonical source PDF changed before result persistence")
            blob = await session.get(Blob, current.blob_id)
            if (
                current.canonical_paper_id != source.canonical_paper_id
                or current.status != "ACTIVE"
                or blob is None
                or blob.sha256 != source.pdf_sha256
            ):
                raise RuntimeError("Canonical source PDF changed before result persistence")
            result_sha256 = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
            existing = await session.scalar(
                select(Artifact).where(
                    Artifact.canonical_paper_id == source.canonical_paper_id,
                    Artifact.artifact_key == "pdf-text",
                    Artifact.artifact_type == "EXTRACTED_TEXT",
                    Artifact.status == "ACTIVE",
                    Artifact.source_fingerprint == source.pdf_sha256,
                )
            )
            if existing is not None:
                existing_blob = await session.get(Blob, existing.blob_id)
                if existing_blob is not None and existing_blob.sha256 == result_sha256:
                    artifacts.append(existing)
                    continue
            text_blob = await blob_service.store_bytes(
                session,
                storage,
                data=result.text.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                actor_principal_id=actor_principal_id,
            )
            artifacts.append(
                await artifact_service.set_canonical(
                    session,
                    canonical_paper_id=source.canonical_paper_id,
                    artifact_key="pdf-text",
                    artifact_type="EXTRACTED_TEXT",
                    blob_id=text_blob.blob_id,
                    media_type="text/markdown; charset=utf-8",
                    actor_principal_id=actor_principal_id,
                    original_filename="pdf-text.md",
                    provenance={
                        "converter": "AsyncBatchPdfText",
                        "source_pdf_blob_id": str(blob.blob_id),
                    },
                    source_fingerprint=blob.sha256,
                )
            )
        return artifacts


canonical_pdf_text_service = CanonicalPdfTextService()
