from __future__ import annotations

import asyncio
import logging
import socket

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import get_object_storage
from backend.app.config import get_settings
from backend.app.database import worker_engine, worker_session_factory
from backend.app.jobs.kernel import LeaseClaim, LeaseWorker
from backend.app.models import DocumentBuildTask

from .embeddings import openai_embedding_client
from .executor import HttpPipelineExecutor, UnavailablePipelineExecutor
from .job_handlers import (
    BuildDocumentTaskHandler,
    BuildEmbeddingsTaskHandler,
    BuildManifestBm25TaskHandler,
    DocumentHandlerRegistry,
    DocumentTaskHandler,
    PdfToTextTaskHandler,
    PublishReleaseTaskHandler,
    ValidateReleaseTaskHandler,
)
from .job_queue import document_task_queue
from .orchestration import INDEX_QUEUE, PIPELINE_QUEUE, SOURCE_QUEUE, document_build_orchestrator
from .sources import HttpPdfTextConverter


class DocumentWorkerBackend:
    def __init__(
        self,
        registry: DocumentHandlerRegistry,
        *,
        queue_names: set[str],
        lease_seconds: int,
    ) -> None:
        self.registry = registry
        self.queue_names = queue_names
        self.lease_seconds = lease_seconds

    async def claim(self, session: AsyncSession, *, worker_id: str) -> LeaseClaim | None:
        task = await document_task_queue.claim(
            session,
            worker_id=worker_id,
            queue_names=self.queue_names,
            task_types=self.registry.task_types,
            lease_seconds=self.lease_seconds,
        )
        return LeaseClaim(task.task_id, task.task_type) if task is not None else None

    async def execute(self, session: AsyncSession, claim: LeaseClaim, *, worker_id: str) -> None:
        task = await session.get(DocumentBuildTask, claim.item_id)
        if task is None:
            return
        await self.registry.require(claim.item_type).handle(session, task, worker_id=worker_id)

    async def fail_unexpected(
        self,
        session: AsyncSession,
        claim: LeaseClaim,
        *,
        worker_id: str,
        error: Exception,
    ) -> None:
        await document_task_queue.fail(
            session,
            claim.item_id,
            worker_id=worker_id,
            error={"code": "DOCUMENT_TASK_ERROR", "type": type(error).__name__},
            retry_delay_seconds=0,
        )


def _registry(*handlers: DocumentTaskHandler) -> DocumentHandlerRegistry:
    registry = DocumentHandlerRegistry()
    for handler in handlers:
        registry.register(handler)
    return registry


async def _run_scheduler(poll_seconds: float) -> None:
    while True:
        async with worker_session_factory() as session:
            await document_build_orchestrator.advance_submitted_runs(session)
            await document_build_orchestrator.enqueue_due_reconciles(session)
            await session.commit()
        await asyncio.sleep(poll_seconds)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.document_pdf_text_url:
        raise RuntimeError("LITV2_DOCUMENT_PDF_TEXT_URL is required")
    hostname = socket.gethostname()
    storage = get_object_storage()
    async with httpx.AsyncClient() as http:
        source_registry = _registry(
            PdfToTextTaskHandler(
                storage,
                HttpPdfTextConverter(
                    http,
                    endpoint=settings.document_pdf_text_url,
                    request_timeout_seconds=settings.document_pdf_text_request_timeout_seconds,
                    job_timeout_seconds=settings.document_pdf_text_job_timeout_seconds,
                    poll_interval_seconds=settings.document_pdf_text_poll_interval_seconds,
                ),
            )
        )
        pipeline_executor = (
            HttpPipelineExecutor(http, endpoint=settings.document_pipeline_url)
            if settings.document_pipeline_url
            else UnavailablePipelineExecutor()
        )
        pipeline_registry = _registry(
            BuildDocumentTaskHandler(
                storage,
                pipeline_executor,
            )
        )
        index_registry = _registry(
            BuildManifestBm25TaskHandler(),
            BuildEmbeddingsTaskHandler(storage, openai_embedding_client(http, settings)),
            ValidateReleaseTaskHandler(),
            PublishReleaseTaskHandler(),
        )
        workers = (
            (
                "source",
                settings.document_source_concurrency,
                DocumentWorkerBackend(
                    source_registry,
                    queue_names={SOURCE_QUEUE},
                    lease_seconds=settings.document_task_lease_seconds,
                ),
            ),
            (
                "pipeline",
                settings.document_pipeline_concurrency,
                DocumentWorkerBackend(
                    pipeline_registry,
                    queue_names={PIPELINE_QUEUE},
                    lease_seconds=settings.document_task_lease_seconds,
                ),
            ),
            (
                "index",
                settings.document_index_concurrency,
                DocumentWorkerBackend(
                    index_registry,
                    queue_names={INDEX_QUEUE},
                    lease_seconds=settings.document_task_lease_seconds,
                ),
            ),
        )
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(_run_scheduler(settings.document_scheduler_poll_seconds))
                for name, concurrency, backend in workers:
                    worker = LeaseWorker(worker_session_factory, backend)
                    for slot in range(concurrency):
                        group.create_task(worker.run_slot(f"{hostname}:document:{name}:{slot}"))
        finally:
            await worker_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
