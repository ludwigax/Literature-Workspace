from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import ObjectStorage
from backend.app.authorization.dependencies import Actor
from backend.app.jobs.service import job_service
from backend.app.models import (
    BackgroundJob,
    Blob,
    LibraryItem,
    Principal,
)

from .citations import LimitedCitation, parse_citation_file
from .limited_items import limited_item_service
from .service import METADATA_REFRESH_JOB

CITATION_IMPORT_JOB = "CITATION_IMPORT"


class CitationImportHandler:
    job_type = CITATION_IMPORT_JOB

    def __init__(self, *, max_bytes: int, storage: ObjectStorage) -> None:
        self.max_bytes = max_bytes
        self.storage = storage

    async def handle(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> None:
        blob_id = uuid.UUID(str(job.payload["blob_id"]))
        filename = str(job.payload["filename"])
        blob = await session.get(Blob, blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise LookupError("Citation import Blob is unavailable")
        data = await self.storage.read_bytes(blob.storage_key, self.max_bytes)
        records = parse_citation_file(data, filename)
        principal = (
            await session.get(Principal, job.actor_principal_id)
            if job.actor_principal_id is not None
            else None
        )
        if principal is None:
            raise LookupError("Citation import actor is unavailable")
        actor = Actor(
            principal_id=principal.principal_id,
            display_name=principal.display_name,
            session_id=job.correlation_id,
        )
        await job_service.progress(
            session,
            job.job_id,
            worker_id=worker_id,
            current=0,
            total=len(records),
            message=f"Importing {len(records)} citation records",
        )
        item_ids: list[uuid.UUID] = []
        refresh_jobs = 0
        for index, record in enumerate(records, start=1):
            item, should_refresh = await self._add_limited_record(
                session,
                actor=actor,
                library_id=job.library_id,
                record=record,
            )
            item_ids.append(item.library_item_id)
            if should_refresh:
                await job_service.enqueue(
                    session,
                    actor,
                    job.library_id,
                    job_type=METADATA_REFRESH_JOB,
                    payload={
                        "library_item_id": str(item.library_item_id),
                        "refresh_mode": "AUTO",
                    },
                    idempotency_key=f"citation:{job.job_id}:{item.library_item_id}",
                    progress_total=2,
                    max_attempts=2,
                )
                refresh_jobs += 1
            await job_service.progress(
                session,
                job.job_id,
                worker_id=worker_id,
                current=index,
                total=len(records),
                message=f"Imported {index} of {len(records)} records",
            )
        await job_service.succeed(
            session,
            job.job_id,
            worker_id=worker_id,
            result={
                "library_item_ids": [str(value) for value in item_ids],
                "record_count": len(item_ids),
                "metadata_refresh_jobs": refresh_jobs,
            },
        )

    @staticmethod
    async def _add_limited_record(
        session: AsyncSession,
        *,
        actor: Actor,
        library_id: uuid.UUID,
        record: LimitedCitation,
    ) -> tuple[LibraryItem, bool]:
        initialized = await limited_item_service.initialize(
            session,
            actor=actor,
            library_id=library_id,
            metadata=record.metadata(),
            doi=record.doi,
        )
        should_refresh = bool(record.doi and initialized.metadata_source == "UNDEFINED")
        return initialized.item, should_refresh
