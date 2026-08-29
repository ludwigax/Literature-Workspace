from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import dataclass
from typing import Protocol

from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import ObjectStorage
from backend.app.authorization.dependencies import Actor
from backend.app.jobs.service import job_service
from backend.app.models import BackgroundJob, Blob, Principal

from .identifiers import select_pdf_identifiers
from .reconcile import IdentifierConflictError, identifier_reconciliation_service
from .service import METADATA_REFRESH_JOB

PDF_IMPORT_JOB = "PDF_IMPORT"


@dataclass(frozen=True, slots=True)
class PdfText:
    text: str
    page_count: int
    pages_examined: int
    metadata_text: str = ""
    page_text: str = ""


class PdfTextExtractor(Protocol):
    async def extract(self, data: bytes) -> PdfText: ...


class PypdfTextExtractor:
    def __init__(self, *, max_pages: int) -> None:
        self.max_pages = max_pages

    async def extract(self, data: bytes) -> PdfText:
        return await asyncio.to_thread(self._extract_sync, data)

    def _extract_sync(self, data: bytes) -> PdfText:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("Encrypted PDF requires a password")
        page_count = len(reader.pages)
        pages_examined = min(page_count, self.max_pages)
        page_text = "\n".join(
            value
            for page in reader.pages[:pages_examined]
            if (value := (page.extract_text() or "").strip())
        )
        metadata_text = "\n".join(
            f"{key}: {value}"
            for key, value in (reader.metadata or {}).items()
            if value is not None and str(value).strip()
        )
        return PdfText(
            text="\n".join(value for value in (metadata_text, page_text) if value),
            page_count=page_count,
            pages_examined=pages_examined,
            metadata_text=metadata_text,
            page_text=page_text,
        )


class PdfImportHandler:
    job_type = PDF_IMPORT_JOB

    def __init__(
        self,
        *,
        max_bytes: int,
        storage: ObjectStorage,
        extractor: PdfTextExtractor,
    ) -> None:
        self.max_bytes = max_bytes
        self.storage = storage
        self.extractor = extractor

    async def handle(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> None:
        blob_id = uuid.UUID(str(job.payload["blob_id"]))
        item_id = uuid.UUID(str(job.payload["library_item_id"]))
        blob = await session.get(Blob, blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise LookupError("PDF Blob is unavailable")

        await job_service.progress(
            session,
            job.job_id,
            worker_id=worker_id,
            current=1,
            total=3,
            message="Reading PDF text",
        )
        data = await self.storage.read_bytes(blob.storage_key, self.max_bytes)
        extracted = await self.extractor.extract(data)
        selected = select_pdf_identifiers(
            extracted.metadata_text,
            extracted.page_text or extracted.text,
            str(job.payload.get("filename") or ""),
        )
        if not selected.identifiers:
            await job_service.succeed(
                session,
                job.job_id,
                worker_id=worker_id,
                result={
                    "library_item_id": str(item_id),
                    "outcome": "NEEDS_REVIEW",
                    "reason": "SCHOLARLY_IDENTIFIER_NOT_FOUND",
                    "page_count": extracted.page_count,
                    "pages_examined": extracted.pages_examined,
                },
            )
            return

        await job_service.progress(
            session,
            job.job_id,
            worker_id=worker_id,
            current=2,
            total=3,
            message="Matching scholarly identifiers",
        )
        try:
            reconciled = await identifier_reconciliation_service.reconcile_identifiers(
                session,
                library_id=job.library_id,
                library_item_id=item_id,
                identifiers=selected.identifiers,
                actor_principal_id=job.actor_principal_id,
            )
        except IdentifierConflictError as error:
            await job_service.succeed(
                session,
                job.job_id,
                worker_id=worker_id,
                result={
                    "library_item_id": str(item_id),
                    "outcome": "NEEDS_REVIEW",
                    "reason": "IDENTIFIER_CONFLICT",
                    "detail": str(error),
                    "page_count": extracted.page_count,
                    "pages_examined": extracted.pages_examined,
                },
            )
            return

        metadata_job_id: str | None = None
        if not reconciled.metadata_already_resolved and selected.metadata_doi is not None:
            principal = (
                await session.get(Principal, job.actor_principal_id)
                if job.actor_principal_id is not None
                else None
            )
            if principal is None:
                raise LookupError("PDF import actor is unavailable")
            actor = Actor(
                principal_id=principal.principal_id,
                display_name=principal.display_name,
                session_id=job.correlation_id,
            )
            metadata_job = await job_service.enqueue(
                session,
                actor,
                job.library_id,
                job_type=METADATA_REFRESH_JOB,
                payload={
                    "library_item_id": str(reconciled.library_item_id),
                    "refresh_mode": "AUTO",
                },
                idempotency_key=f"pdf:{job.job_id}:{reconciled.library_item_id}",
                progress_total=2,
                max_attempts=2,
            )
            metadata_job_id = str(metadata_job.job_id)

        await job_service.succeed(
            session,
            job.job_id,
            worker_id=worker_id,
            result={
                "library_item_id": str(reconciled.library_item_id),
                "canonical_paper_id": str(reconciled.canonical_paper_id),
                "doi": selected.metadata_doi,
                "identifiers": [
                    {"scheme": value.scheme, "value": value.normalized_value}
                    for value in selected.identifiers
                ],
                "identifier_evidence": selected.evidence_source,
                "outcome": (
                    "READY"
                    if reconciled.metadata_already_resolved
                    else "METADATA_QUEUED"
                    if selected.metadata_doi is not None
                    else "NEEDS_REVIEW"
                ),
                "merged_item": reconciled.merged_item,
                "metadata_job_id": metadata_job_id,
                "page_count": extracted.page_count,
                "pages_examined": extracted.pages_examined,
            },
        )
