from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    DocumentBuildRun,
    DocumentBuildTask,
    DocumentBuildTaskAttempt,
    DocumentDatabase,
    DocumentDatabaseRelease,
)


class DocumentTaskQueue:
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        task_type: str,
        queue_name: str,
        subject_key: str,
        payload: dict[str, Any],
        progress_total: int = 1,
        max_attempts: int = 3,
    ) -> DocumentBuildTask:
        existing = await session.scalar(
            select(DocumentBuildTask).where(
                DocumentBuildTask.run_id == run_id,
                DocumentBuildTask.task_type == task_type,
                DocumentBuildTask.subject_key == subject_key,
            )
        )
        if existing is not None:
            return existing
        task = DocumentBuildTask(
            run_id=run_id,
            task_type=task_type,
            queue_name=queue_name,
            subject_key=subject_key,
            status="PENDING",
            payload=payload,
            progress_current=0,
            progress_total=max(1, progress_total),
            attempt_count=0,
            max_attempts=max(1, max_attempts),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        await session.flush()
        return task

    async def claim(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        queue_names: set[str],
        task_types: set[str],
        lease_seconds: int = 300,
    ) -> DocumentBuildTask | None:
        now = datetime.now(UTC)
        statement = (
            select(DocumentBuildTask)
            .join(DocumentBuildRun, DocumentBuildRun.run_id == DocumentBuildTask.run_id)
            .where(
                DocumentBuildRun.status == "RUNNING",
                DocumentBuildTask.queue_name.in_(queue_names),
                DocumentBuildTask.task_type.in_(task_types),
                DocumentBuildTask.attempt_count < DocumentBuildTask.max_attempts,
                or_(
                    (DocumentBuildTask.status == "PENDING")
                    & (DocumentBuildTask.available_at <= now),
                    (DocumentBuildTask.status == "RUNNING")
                    & (DocumentBuildTask.lease_expires_at < now),
                ),
            )
            .order_by(
                DocumentBuildTask.available_at,
                DocumentBuildTask.created_at,
                DocumentBuildTask.task_id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        task = await session.scalar(statement)
        if task is None:
            return None
        task.status = "RUNNING"
        task.lease_owner = worker_id
        task.lease_expires_at = now + timedelta(seconds=max(30, lease_seconds))
        task.attempt_count += 1
        task.error = None
        session.add(
            DocumentBuildTaskAttempt(
                task_id=task.task_id,
                attempt_number=task.attempt_count,
                worker_id=worker_id,
                started_at=now,
            )
        )
        await session.flush()
        return task

    async def progress(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        worker_id: str,
        current: int,
        total: int,
        message: str | None,
        lease_seconds: int = 300,
    ) -> None:
        task = await self.require_owned(session, task_id, worker_id=worker_id, lock=True)
        if current < task.progress_current or total < 1 or current > total:
            raise RuntimeError("Invalid Document task progress")
        task.progress_current = current
        task.progress_total = total
        task.progress_message = message
        task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=max(30, lease_seconds))
        await session.flush()

    async def succeed(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        worker_id: str,
        result: dict[str, Any],
    ) -> DocumentBuildTask:
        task = await self.require_owned(session, task_id, worker_id=worker_id, lock=True)
        task.status = "SUCCEEDED"
        task.result = result
        task.progress_current = task.progress_total
        task.progress_message = "Completed"
        task.lease_owner = None
        task.lease_expires_at = None
        attempt = await self.current_attempt(session, task)
        attempt.finished_at = datetime.now(UTC)
        attempt.outcome = "SUCCEEDED"
        await session.flush()
        return task

    async def fail(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        worker_id: str,
        error: dict[str, Any],
        retry_delay_seconds: int = 5,
    ) -> DocumentBuildTask:
        task = await self.require_owned(session, task_id, worker_id=worker_id, lock=True)
        retrying = task.attempt_count < task.max_attempts
        task.status = "PENDING" if retrying else "FAILED"
        task.error = error
        task.available_at = datetime.now(UTC) + timedelta(seconds=max(0, retry_delay_seconds))
        task.lease_owner = None
        task.lease_expires_at = None
        attempt = await self.current_attempt(session, task)
        attempt.finished_at = datetime.now(UTC)
        attempt.outcome = "RETRY_SCHEDULED" if retrying else "FAILED"
        attempt.error = error
        if not retrying:
            await self._fail_run(session, task.run_id, error)
        await session.flush()
        return task

    @staticmethod
    async def require_owned(
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        worker_id: str,
        lock: bool,
    ) -> DocumentBuildTask:
        statement = select(DocumentBuildTask).where(
            DocumentBuildTask.task_id == task_id,
            DocumentBuildTask.status == "RUNNING",
            DocumentBuildTask.lease_owner == worker_id,
        )
        if lock:
            statement = statement.with_for_update()
        task = await session.scalar(statement)
        if task is None:
            raise RuntimeError("Document task lease is not owned by this worker")
        return task

    @staticmethod
    async def current_attempt(
        session: AsyncSession, task: DocumentBuildTask
    ) -> DocumentBuildTaskAttempt:
        attempt = await session.scalar(
            select(DocumentBuildTaskAttempt).where(
                DocumentBuildTaskAttempt.task_id == task.task_id,
                DocumentBuildTaskAttempt.attempt_number == task.attempt_count,
            )
        )
        if attempt is None:
            raise RuntimeError("Current Document task attempt is missing")
        return attempt

    @staticmethod
    async def _fail_run(session: AsyncSession, run_id: uuid.UUID, error: dict[str, Any]) -> None:
        run = await session.get(DocumentBuildRun, run_id, with_for_update=True)
        if run is None or run.status != "RUNNING":
            return
        now = datetime.now(UTC)
        run.status = "FAILED"
        run.error = error
        run.finished_at = now
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


document_task_queue = DocumentTaskQueue()
