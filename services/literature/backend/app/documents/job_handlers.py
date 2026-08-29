from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import ObjectStorage
from backend.app.models import DocumentBuildRun, DocumentBuildTask

from .embeddings import EmbeddingClient, document_embedding_service
from .executor import PipelineExecutor
from .indexing import document_index_service
from .job_queue import document_task_queue
from .orchestration import (
    BUILD_DOCUMENT,
    BUILD_EMBEDDINGS,
    BUILD_MANIFEST_BM25,
    PDF_TO_TEXT,
    PUBLISH_RELEASE,
    VALIDATE_RELEASE,
    document_build_orchestrator,
)
from .service import document_domain_service
from .sources import PdfTextConverter, PdfTextRemoteJobError, canonical_pdf_text_service


class DocumentTaskHandler(Protocol):
    task_type: str

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None: ...


class DocumentHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, DocumentTaskHandler] = {}

    def register(self, handler: DocumentTaskHandler) -> None:
        if handler.task_type in self._handlers:
            raise ValueError(f"Document handler already registered: {handler.task_type}")
        self._handlers[handler.task_type] = handler

    def require(self, task_type: str) -> DocumentTaskHandler:
        try:
            return self._handlers[task_type]
        except KeyError as error:
            raise LookupError(f"No Document handler registered for {task_type}") from error

    @property
    def task_types(self) -> set[str]:
        return set(self._handlers)


async def _complete(
    session: AsyncSession,
    task: DocumentBuildTask,
    *,
    worker_id: str,
    result: dict[str, object],
) -> None:
    await document_task_queue.succeed(session, task.task_id, worker_id=worker_id, result=result)
    await document_build_orchestrator.advance(session, task.run_id)


class PdfToTextTaskHandler:
    task_type = PDF_TO_TEXT

    def __init__(self, storage: ObjectStorage, converter: PdfTextConverter) -> None:
        self.storage = storage
        self.converter = converter

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None:
        raw_items = task.payload.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 4:
            raise RuntimeError("PDF_TO_TEXT task must contain one to four sources")
        entries = [
            (
                uuid.UUID(str(value["canonical_paper_id"])),
                uuid.UUID(str(value["source_artifact_id"])),
            )
            for value in raw_items
            if isinstance(value, dict)
        ]
        if len(entries) != len(raw_items):
            raise RuntimeError("PDF_TO_TEXT task contains an invalid source entry")
        task_id = task.task_id
        run_id = task.run_id
        sources = await canonical_pdf_text_service.prepare_batch(
            session,
            self.storage,
            entries,
        )
        remote_job_id = task.payload.get("remote_job_id")
        if not isinstance(remote_job_id, str) or not remote_job_id:
            remote_job_id = await self.converter.submit(sources)
            task.payload = {**task.payload, "remote_job_id": remote_job_id}
            task.progress_message = f"Remote PDF job {remote_job_id} submitted"
            await session.commit()
        else:
            await session.rollback()
        try:
            results = await self.converter.wait_and_fetch(remote_job_id, sources)
        except PdfTextRemoteJobError:
            failed_task = await session.get(DocumentBuildTask, task_id, with_for_update=True)
            if failed_task is not None:
                failed_task.payload = {
                    key: value
                    for key, value in failed_task.payload.items()
                    if key != "remote_job_id"
                }
                await session.commit()
            raise
        run = await session.get(DocumentBuildRun, run_id)
        artifacts = await canonical_pdf_text_service.persist_batch(
            session,
            self.storage,
            sources,
            results,
            actor_principal_id=run.actor_principal_id if run is not None else None,
        )
        reloaded_task = await session.get(DocumentBuildTask, task_id)
        if reloaded_task is None:
            raise RuntimeError("PDF_TO_TEXT task disappeared before completion")
        await _complete(
            session,
            reloaded_task,
            worker_id=worker_id,
            result={
                "remote_job_id": remote_job_id,
                "artifact_ids": [str(artifact.artifact_id) for artifact in artifacts],
            },
        )


class BuildDocumentTaskHandler:
    task_type = BUILD_DOCUMENT

    def __init__(self, storage: ObjectStorage, executor: PipelineExecutor) -> None:
        self.storage = storage
        self.executor = executor

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None:
        run = await session.get(DocumentBuildRun, task.run_id)
        document = await document_domain_service.execute_item(
            session,
            self.storage,
            self.executor,
            release_id=uuid.UUID(str(task.payload["release_id"])),
            canonical_paper_id=uuid.UUID(str(task.payload["canonical_paper_id"])),
            actor_principal_id=run.actor_principal_id if run is not None else None,
        )
        await _complete(
            session,
            task,
            worker_id=worker_id,
            result={"document_id": str(document.document_id)},
        )


class BuildManifestBm25TaskHandler:
    task_type = BUILD_MANIFEST_BM25

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None:
        index = await document_index_service.build_release_index(
            session, uuid.UUID(str(task.payload["release_id"]))
        )
        await _complete(
            session,
            task,
            worker_id=worker_id,
            result={"row_count": index.row_count, "manifest_hash": index.manifest_hash},
        )


class BuildEmbeddingsTaskHandler:
    task_type = BUILD_EMBEDDINGS

    def __init__(self, storage: ObjectStorage, client: EmbeddingClient) -> None:
        self.storage = storage
        self.client = client

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None:
        index = await document_embedding_service.build_release_embeddings(
            session,
            self.storage,
            self.client,
            release_id=uuid.UUID(str(task.payload["release_id"])),
        )
        await _complete(
            session,
            task,
            worker_id=worker_id,
            result={
                "row_count": index.row_count,
                "embedding_blob_id": str(index.embedding_index_blob_id),
            },
        )


class ValidateReleaseTaskHandler:
    task_type = VALIDATE_RELEASE

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None:
        result = await document_build_orchestrator.validate_release(session, task.run_id)
        await _complete(session, task, worker_id=worker_id, result=result)


class PublishReleaseTaskHandler:
    task_type = PUBLISH_RELEASE

    async def handle(
        self,
        session: AsyncSession,
        task: DocumentBuildTask,
        *,
        worker_id: str,
    ) -> None:
        run = await session.get(DocumentBuildRun, task.run_id)
        if run is None or run.release_id is None:
            raise RuntimeError("Document BuildRun has no publishable Release")
        release = await document_domain_service.publish_release(
            session,
            run.release_id,
            actor_principal_id=run.actor_principal_id,
        )
        await _complete(
            session,
            task,
            worker_id=worker_id,
            result={"release_id": str(release.release_id)},
        )
