from __future__ import annotations

import asyncio
import logging
import socket

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import get_object_storage
from backend.app.config import get_settings
from backend.app.database import worker_engine, worker_session_factory
from backend.app.ingestion.citation_import import CitationImportHandler
from backend.app.ingestion.handlers import MetadataRefreshHandler
from backend.app.ingestion.pdf_import import PdfImportHandler, PypdfTextExtractor
from backend.app.ingestion.providers import get_metadata_resolver
from backend.app.ingestion.zotero_import import ZoteroImportHandler
from backend.app.jobs.handlers import HandlerRegistry
from backend.app.models import BackgroundJob

from .kernel import LeaseClaim, LeaseWorker
from .service import job_service

logger = logging.getLogger(__name__)


def default_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(MetadataRefreshHandler(get_metadata_resolver()))
    settings = get_settings()
    registry.register(
        CitationImportHandler(
            max_bytes=settings.citation_import_max_bytes,
            storage=get_object_storage(),
        )
    )
    registry.register(
        PdfImportHandler(
            max_bytes=settings.pdf_import_max_bytes,
            storage=get_object_storage(),
            extractor=PypdfTextExtractor(max_pages=settings.pdf_extract_max_pages),
        )
    )
    registry.register(
        ZoteroImportHandler(
            max_bytes=settings.zotero_import_max_bytes,
            storage=get_object_storage(),
        )
    )
    return registry


class JobWorker:
    def __init__(self, registry: HandlerRegistry) -> None:
        self.registry = registry
        self._worker = LeaseWorker(worker_session_factory, _LibraryWorkerBackend(registry))

    async def run_once(self, worker_id: str) -> bool:
        return await self._worker.run_once(worker_id)

    async def run_slot(self, worker_id: str) -> None:
        await self._worker.run_slot(worker_id)


class _LibraryWorkerBackend:
    def __init__(self, registry: HandlerRegistry) -> None:
        self.registry = registry

    async def claim(self, session: AsyncSession, *, worker_id: str) -> LeaseClaim | None:
        job = await job_service.claim(
            session,
            worker_id=worker_id,
            lease_seconds=120,
            job_types=self.registry.job_types,
        )
        return LeaseClaim(job.job_id, job.job_type) if job is not None else None

    async def execute(self, session: AsyncSession, claim: LeaseClaim, *, worker_id: str) -> None:
        job = await session.get(BackgroundJob, claim.item_id)
        if job is None:
            return
        await self.registry.require(claim.item_type).handle(session, job, worker_id=worker_id)

    async def fail_unexpected(
        self,
        session: AsyncSession,
        claim: LeaseClaim,
        *,
        worker_id: str,
        error: Exception,
    ) -> None:
        await job_service.fail(
            session,
            claim.item_id,
            worker_id=worker_id,
            error={"code": "UNEXPECTED_WORKER_ERROR", "type": type(error).__name__},
            retry_delay_seconds=0,
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    worker = JobWorker(default_registry())
    hostname = socket.gethostname()
    try:
        async with asyncio.TaskGroup() as group:
            for slot in range(settings.worker_concurrency):
                group.create_task(worker.run_slot(f"{hostname}:{slot}"))
    finally:
        await worker_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
