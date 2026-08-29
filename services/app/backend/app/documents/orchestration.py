from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    Artifact,
    Blob,
    DocumentBuildRun,
    DocumentBuildTask,
    DocumentDatabase,
    DocumentDatabaseRelease,
    DocumentIndexManifestRow,
    DocumentPipeline,
    DocumentReleaseEntry,
    DocumentReleaseIndex,
)

from .job_queue import document_task_queue
from .service import document_domain_service

PDF_TO_TEXT = "PDF_TO_TEXT"
PDF_TEXT_BATCH_SIZE = 4
BUILD_DOCUMENT = "BUILD_DOCUMENT"
BUILD_MANIFEST_BM25 = "BUILD_MANIFEST_BM25"
BUILD_EMBEDDINGS = "BUILD_EMBEDDINGS"
VALIDATE_RELEASE = "VALIDATE_RELEASE"
PUBLISH_RELEASE = "PUBLISH_RELEASE"

SOURCE_QUEUE = "document.source"
PIPELINE_QUEUE = "document.pipeline"
INDEX_QUEUE = "document.index"

_PHASE_TASK_TYPES = {
    "SOURCE_PREPARATION": {PDF_TO_TEXT},
    "DOCUMENTS": {BUILD_DOCUMENT},
    "MANIFEST": {BUILD_MANIFEST_BM25},
    "EMBEDDINGS": {BUILD_EMBEDDINGS},
    "VALIDATION": {VALIDATE_RELEASE},
    "PUBLISH": {PUBLISH_RELEASE},
}


class DocumentBuildOrchestrator:
    async def start_build(
        self,
        session: AsyncSession,
        database_id: uuid.UUID,
        *,
        build_mode: str = "UPDATE",
        trigger_reason: str = "MANUAL",
        actor_principal_id: uuid.UUID | None = None,
        defer_advance: bool = False,
    ) -> DocumentBuildRun:
        mode = build_mode.strip().upper()
        if mode not in {"FULL", "UPDATE"}:
            raise ValueError("build_mode must be FULL or UPDATE")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"document-build:{database_id}"},
        )
        active = await session.scalar(
            select(DocumentBuildRun)
            .where(
                DocumentBuildRun.database_id == database_id,
                DocumentBuildRun.status == "RUNNING",
            )
            .with_for_update()
        )
        if active is not None:
            active_database = await session.get(DocumentDatabase, database_id)
            active_pipeline = (
                await session.get(DocumentPipeline, active_database.pipeline_id)
                if active_database is not None
                else None
            )
            changed = (
                active_database is None
                or active_database.range_revision != active.range_revision
                or active_pipeline is None
                or active_pipeline.active_version_id != active.pipeline_version_id
                or (mode == "FULL" and active.build_mode != "FULL")
            )
            if changed:
                active.reconcile_requested = True
            await session.flush()
            return active

        database = await session.get(DocumentDatabase, database_id, with_for_update=True)
        if database is None:
            raise LookupError("Document Database not found")
        pipeline = await session.get(DocumentPipeline, database.pipeline_id)
        if pipeline is None or pipeline.active_version_id is None:
            raise RuntimeError("Document Database Pipeline has no active version")
        run = DocumentBuildRun(
            database_id=database_id,
            pipeline_version_id=pipeline.active_version_id,
            range_revision=database.range_revision,
            build_mode=mode,
            trigger_reason=trigger_reason.strip().upper(),
            status="RUNNING",
            phase="SOURCE_PREPARATION",
            reconcile_requested=False,
            actor_principal_id=actor_principal_id,
        )
        session.add(run)
        await session.flush()
        paper_ids = await document_domain_service.resolve_scope(session, database)
        missing: list[str] = []
        pending_sources: list[tuple[uuid.UUID, Artifact]] = []
        for paper_id in paper_ids:
            if await self._available_artifact(session, paper_id, "pdf-text", "EXTRACTED_TEXT"):
                continue
            pdf = await self._available_artifact(session, paper_id, "pdf", "SOURCE_PDF")
            if pdf is None:
                missing.append(str(paper_id))
                continue
            pending_sources.append((paper_id, pdf))
        if missing:
            run.status = "FAILED"
            run.error = {"code": "CANONICAL_SOURCE_MISSING", "paper_ids": missing}
            run.finished_at = datetime.now(UTC)
            await session.flush()
            return run
        for offset in range(0, len(pending_sources), PDF_TEXT_BATCH_SIZE):
            batch = pending_sources[offset : offset + PDF_TEXT_BATCH_SIZE]
            await document_task_queue.enqueue(
                session,
                run_id=run.run_id,
                task_type=PDF_TO_TEXT,
                queue_name=SOURCE_QUEUE,
                subject_key=f"batch:{offset // PDF_TEXT_BATCH_SIZE:06d}",
                payload={
                    "items": [
                        {
                            "canonical_paper_id": str(paper_id),
                            "source_artifact_id": str(pdf.artifact_id),
                            "source_fingerprint": pdf.source_fingerprint,
                        }
                        for paper_id, pdf in batch
                    ]
                },
                progress_total=len(batch),
                max_attempts=3,
            )
        if not defer_advance:
            await self.advance(session, run.run_id)
        return run

    async def advance(self, session: AsyncSession, run_id: uuid.UUID) -> DocumentBuildRun:
        run = await session.get(DocumentBuildRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError("Document BuildRun not found")
        while run.status == "RUNNING":
            current_types = _PHASE_TASK_TYPES[run.phase]
            statuses = list(
                await session.scalars(
                    select(DocumentBuildTask.status).where(
                        DocumentBuildTask.run_id == run_id,
                        DocumentBuildTask.task_type.in_(current_types),
                    )
                )
            )
            if any(status == "FAILED" for status in statuses):
                run.status = "FAILED"
                run.error = {"code": "DOCUMENT_BUILD_TASK_FAILED", "phase": run.phase}
                run.finished_at = datetime.now(UTC)
                break
            if any(status in {"PENDING", "RUNNING"} for status in statuses):
                break

            if run.phase == "SOURCE_PREPARATION":
                if not await self._inputs_still_pinned(session, run):
                    run.status = "CANCELLED"
                    run.result = {"outcome": "STALE_INPUT"}
                    run.finished_at = datetime.now(UTC)
                    await session.flush()
                    return await self.start_build(
                        session,
                        run.database_id,
                        build_mode=run.build_mode,
                        trigger_reason="COALESCED_CHANGE",
                        actor_principal_id=run.actor_principal_id,
                    )
                release = await document_domain_service.start_reconcile(
                    session,
                    run.database_id,
                    build_mode=run.build_mode,
                    trigger_reason=run.trigger_reason,
                )
                if release is None:
                    await self._finish_no_change(session, run)
                    break
                run.release_id = release.release_id
                run.phase = "DOCUMENTS"
                paper_ids = list(
                    await session.scalars(
                        select(DocumentReleaseEntry.canonical_paper_id).where(
                            DocumentReleaseEntry.release_id == release.release_id,
                            DocumentReleaseEntry.status == "PENDING",
                        )
                    )
                )
                for paper_id in paper_ids:
                    await document_task_queue.enqueue(
                        session,
                        run_id=run_id,
                        task_type=BUILD_DOCUMENT,
                        queue_name=PIPELINE_QUEUE,
                        subject_key=str(paper_id),
                        payload={
                            "release_id": str(release.release_id),
                            "canonical_paper_id": str(paper_id),
                        },
                        max_attempts=3,
                    )
                continue

            if run.phase == "DOCUMENTS":
                run.phase = "MANIFEST"
                await self._enqueue_release_task(session, run, BUILD_MANIFEST_BM25, INDEX_QUEUE)
                continue
            if run.phase == "MANIFEST":
                release = await self._release(session, run)
                if release.embedding_profile:
                    run.phase = "EMBEDDINGS"
                    await self._enqueue_release_task(session, run, BUILD_EMBEDDINGS, INDEX_QUEUE)
                else:
                    run.phase = "VALIDATION"
                    await self._enqueue_release_task(session, run, VALIDATE_RELEASE, INDEX_QUEUE)
                continue
            if run.phase == "EMBEDDINGS":
                run.phase = "VALIDATION"
                await self._enqueue_release_task(session, run, VALIDATE_RELEASE, INDEX_QUEUE)
                continue
            if run.phase == "VALIDATION":
                run.phase = "PUBLISH"
                await self._enqueue_release_task(session, run, PUBLISH_RELEASE, INDEX_QUEUE)
                continue
            if run.phase == "PUBLISH":
                rerun = run.reconcile_requested
                run.phase = "COMPLETED"
                run.status = "SUCCEEDED"
                run.result = {"outcome": "PUBLISHED", "release_id": str(run.release_id)}
                run.finished_at = datetime.now(UTC)
                await session.flush()
                if rerun:
                    return await self.start_build(
                        session,
                        run.database_id,
                        build_mode="UPDATE",
                        trigger_reason="COALESCED_CHANGE",
                        actor_principal_id=run.actor_principal_id,
                    )
                break
        await session.flush()
        return run

    async def validate_release(self, session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
        run = await session.get(DocumentBuildRun, run_id)
        if run is None:
            raise LookupError("Document BuildRun not found")
        release = await self._release(session, run)
        statuses = list(
            await session.scalars(
                select(DocumentReleaseEntry.status).where(
                    DocumentReleaseEntry.release_id == release.release_id
                )
            )
        )
        if len(statuses) != release.expected_count or any(
            status not in {"REUSED", "SUCCEEDED"} for status in statuses
        ):
            raise RuntimeError("Release Document entries are incomplete")
        index = await session.get(DocumentReleaseIndex, release.release_id)
        if index is None or index.status != "READY" or index.bm25_status != "READY":
            raise RuntimeError("Release BM25/Manifest index is incomplete")
        manifest_count = int(
            await session.scalar(
                select(func.count(DocumentIndexManifestRow.row_number)).where(
                    DocumentIndexManifestRow.release_id == release.release_id
                )
            )
            or 0
        )
        if manifest_count != index.row_count:
            raise RuntimeError("Release Manifest row count is inconsistent")
        if release.embedding_profile and (
            index.embedding_status != "READY"
            or index.embedding_index_blob_id is None
            or index.embedding_dimensions is None
        ):
            raise RuntimeError("Release FAISS index is incomplete")
        return {
            "release_id": str(release.release_id),
            "document_count": len(statuses),
            "chunk_count": index.row_count,
            "embedding_status": index.embedding_status,
        }

    async def cancel(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        *,
        actor_principal_id: uuid.UUID,
    ) -> DocumentBuildRun:
        run = await session.get(DocumentBuildRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError("Document BuildRun not found")
        if run.status != "RUNNING":
            return run
        run.status = "CANCELLED"
        run.error = {
            "code": "CANCELLED_BY_ADMIN",
            "actor_principal_id": str(actor_principal_id),
        }
        run.finished_at = datetime.now(UTC)
        await session.execute(
            update(DocumentBuildTask)
            .where(
                DocumentBuildTask.run_id == run_id,
                DocumentBuildTask.status == "PENDING",
            )
            .values(status="CANCELLED")
        )
        if run.release_id is not None:
            release = await session.get(
                DocumentDatabaseRelease, run.release_id, with_for_update=True
            )
            if release is not None and release.status == "BUILDING":
                release.status = "FAILED"
            database = await session.get(DocumentDatabase, run.database_id, with_for_update=True)
            if database is not None and database.building_release_id == run.release_id:
                database.building_release_id = None
        await session.flush()
        return run

    async def retry(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        *,
        actor_principal_id: uuid.UUID,
        defer_advance: bool = False,
    ) -> DocumentBuildRun:
        old = await session.get(DocumentBuildRun, run_id)
        if old is None:
            raise LookupError("Document BuildRun not found")
        if old.status not in {"FAILED", "CANCELLED"}:
            raise RuntimeError("Only a failed or cancelled BuildRun can be retried")
        return await self.start_build(
            session,
            old.database_id,
            build_mode=old.build_mode,
            trigger_reason="RETRY",
            actor_principal_id=actor_principal_id,
            defer_advance=defer_advance,
        )

    async def advance_submitted_runs(
        self, session: AsyncSession, *, limit: int = 20
    ) -> int:
        """Let the worker role advance API-submitted runs with no source tasks."""
        run_ids = list(
            await session.scalars(
                select(DocumentBuildRun.run_id)
                .where(
                    DocumentBuildRun.status == "RUNNING",
                    DocumentBuildRun.phase == "SOURCE_PREPARATION",
                )
                .order_by(DocumentBuildRun.created_at, DocumentBuildRun.run_id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for run_id in run_ids:
            await self.advance(session, run_id)
        return len(run_ids)

    async def set_reconcile_policy(
        self,
        session: AsyncSession,
        database_id: uuid.UUID,
        *,
        enabled: bool,
    ) -> DocumentDatabase:
        database = await session.get(DocumentDatabase, database_id, with_for_update=True)
        if database is None:
            raise LookupError("Document Database not found")
        database.auto_reconcile_enabled = enabled
        database.next_reconcile_at = datetime.now(UTC) + timedelta(days=1) if enabled else None
        await session.flush()
        return database

    async def enqueue_due_reconciles(self, session: AsyncSession, *, limit: int = 20) -> int:
        now = datetime.now(UTC)
        databases = list(
            await session.scalars(
                select(DocumentDatabase)
                .where(
                    DocumentDatabase.auto_reconcile_enabled.is_(True),
                    DocumentDatabase.next_reconcile_at <= now,
                )
                .order_by(DocumentDatabase.next_reconcile_at, DocumentDatabase.database_id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for database in databases:
            database.last_reconcile_checked_at = now
            database.next_reconcile_at = now + timedelta(days=1)
            await self.start_build(
                session,
                database.database_id,
                build_mode="UPDATE",
                trigger_reason="SCHEDULED",
            )
        await session.flush()
        return len(databases)

    @staticmethod
    async def _available_artifact(
        session: AsyncSession,
        paper_id: uuid.UUID,
        artifact_key: str,
        artifact_type: str,
    ) -> Artifact | None:
        artifact: Artifact | None = await session.scalar(
            select(Artifact)
            .join(Blob, Blob.blob_id == Artifact.blob_id)
            .where(
                Artifact.canonical_paper_id == paper_id,
                Artifact.artifact_key == artifact_key,
                Artifact.artifact_type == artifact_type,
                Artifact.status == "ACTIVE",
                Blob.status == "AVAILABLE",
            )
        )
        return artifact

    @staticmethod
    async def _inputs_still_pinned(session: AsyncSession, run: DocumentBuildRun) -> bool:
        database = await session.get(DocumentDatabase, run.database_id)
        if database is None or database.range_revision != run.range_revision:
            return False
        pipeline = await session.get(DocumentPipeline, database.pipeline_id)
        return pipeline is not None and pipeline.active_version_id == run.pipeline_version_id

    @staticmethod
    async def _release(session: AsyncSession, run: DocumentBuildRun) -> DocumentDatabaseRelease:
        if run.release_id is None:
            raise RuntimeError("Document BuildRun has no Release")
        release = await session.get(DocumentDatabaseRelease, run.release_id)
        if release is None:
            raise RuntimeError("Document BuildRun Release is missing")
        return release

    @staticmethod
    async def _enqueue_release_task(
        session: AsyncSession,
        run: DocumentBuildRun,
        task_type: str,
        queue_name: str,
    ) -> None:
        if run.release_id is None:
            raise RuntimeError("Document BuildRun has no Release")
        await document_task_queue.enqueue(
            session,
            run_id=run.run_id,
            task_type=task_type,
            queue_name=queue_name,
            subject_key="release",
            payload={"release_id": str(run.release_id)},
            max_attempts=3,
        )

    async def _finish_no_change(self, session: AsyncSession, run: DocumentBuildRun) -> None:
        rerun = run.reconcile_requested
        run.phase = "COMPLETED"
        run.status = "SUCCEEDED"
        run.result = {"outcome": "NO_CHANGE"}
        run.finished_at = datetime.now(UTC)
        await session.flush()
        if rerun:
            await self.start_build(
                session,
                run.database_id,
                build_mode="UPDATE",
                trigger_reason="COALESCED_CHANGE",
                actor_principal_id=run.actor_principal_id,
            )


document_build_orchestrator = DocumentBuildOrchestrator()
