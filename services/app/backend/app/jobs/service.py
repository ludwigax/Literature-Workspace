from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization.dependencies import Actor, membership_for
from ..models import BackgroundJob, JobAttempt, OutboxEvent


class JobService:
    async def enqueue(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        progress_total: int = 1,
        max_attempts: int = 5,
    ) -> BackgroundJob:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        clean_job_type = job_type.strip()
        if not clean_job_type:
            raise HTTPException(status_code=422, detail="job_type is required")
        clean_key = idempotency_key.strip() if idempotency_key else None
        if clean_key:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"job:{library_id}:{clean_job_type}:{clean_key}"},
            )
            existing = await session.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.library_id == library_id,
                    BackgroundJob.job_type == clean_job_type,
                    BackgroundJob.idempotency_key == clean_key,
                )
            )
            if existing is not None:
                return existing
        now = datetime.now(UTC)
        job = BackgroundJob(
            library_id=library_id,
            job_type=clean_job_type,
            status="PENDING",
            payload=payload,
            progress_current=0,
            progress_total=max(1, progress_total),
            attempt_count=0,
            max_attempts=max(1, max_attempts),
            available_at=now,
            idempotency_key=clean_key,
            actor_principal_id=actor.principal_id,
        )
        session.add(job)
        await session.flush()
        self.emit(
            session,
            library_id=library_id,
            aggregate_type="BackgroundJob",
            aggregate_id=job.job_id,
            event_type="job.enqueued",
            payload={"job_type": clean_job_type},
        )
        return job

    async def claim(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        job_types: set[str] | None = None,
    ) -> BackgroundJob | None:
        now = datetime.now(UTC)
        statement = (
            select(BackgroundJob)
            .where(
                or_(
                    (BackgroundJob.status == "PENDING") & (BackgroundJob.available_at <= now),
                    (BackgroundJob.status == "RUNNING") & (BackgroundJob.lease_expires_at < now),
                ),
                BackgroundJob.attempt_count < BackgroundJob.max_attempts,
            )
            .order_by(BackgroundJob.available_at, BackgroundJob.created_at, BackgroundJob.job_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job_types:
            statement = statement.where(BackgroundJob.job_type.in_(job_types))
        job = await session.scalar(statement)
        if job is None:
            return None
        job.status = "RUNNING"
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(seconds=max(10, lease_seconds))
        job.attempt_count += 1
        job.error = None
        session.add(
            JobAttempt(
                library_id=job.library_id,
                job_id=job.job_id,
                attempt_number=job.attempt_count,
                worker_id=worker_id,
                started_at=now,
            )
        )
        await session.flush()
        return job

    async def progress(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        current: int,
        total: int,
        message: str | None,
        lease_seconds: int = 60,
    ) -> None:
        job = await self.require_worker_job(session, job_id, worker_id=worker_id, lock=True)
        if current < job.progress_current:
            raise RuntimeError("Job progress cannot move backwards within an attempt")
        if total < 1 or current > total:
            raise RuntimeError("Invalid job progress")
        job.progress_current = current
        job.progress_total = total
        job.progress_message = message
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=max(10, lease_seconds))
        await session.flush()

    async def succeed(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        result: dict[str, Any],
    ) -> None:
        job = await self.require_worker_job(session, job_id, worker_id=worker_id, lock=True)
        now = datetime.now(UTC)
        job.status = "SUCCEEDED"
        job.result = result
        job.progress_current = job.progress_total
        job.progress_message = "Completed"
        job.lease_owner = None
        job.lease_expires_at = None
        attempt = await self.current_attempt(session, job)
        attempt.finished_at = now
        attempt.outcome = "SUCCEEDED"
        self.emit(
            session,
            library_id=job.library_id,
            aggregate_type="BackgroundJob",
            aggregate_id=job.job_id,
            event_type="job.succeeded",
            payload={"job_type": job.job_type},
        )
        await session.flush()

    async def fail(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        error: dict[str, Any],
        retry_delay_seconds: int = 5,
    ) -> None:
        job = await self.require_worker_job(session, job_id, worker_id=worker_id, lock=True)
        now = datetime.now(UTC)
        retrying = job.attempt_count < job.max_attempts
        job.status = "PENDING" if retrying else "FAILED"
        job.error = error
        job.available_at = now + timedelta(seconds=max(0, retry_delay_seconds))
        job.lease_owner = None
        job.lease_expires_at = None
        attempt = await self.current_attempt(session, job)
        attempt.finished_at = now
        attempt.outcome = "RETRY_SCHEDULED" if retrying else "FAILED"
        attempt.error = error
        self.emit(
            session,
            library_id=job.library_id,
            aggregate_type="BackgroundJob",
            aggregate_id=job.job_id,
            event_type="job.retry_scheduled" if retrying else "job.failed",
            payload={"job_type": job.job_type, "attempt": job.attempt_count},
        )
        await session.flush()

    @staticmethod
    async def require_worker_job(
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        lock: bool,
    ) -> BackgroundJob:
        statement = select(BackgroundJob).where(
            BackgroundJob.job_id == job_id,
            BackgroundJob.status == "RUNNING",
            BackgroundJob.lease_owner == worker_id,
        )
        if lock:
            statement = statement.with_for_update()
        job = await session.scalar(statement)
        if job is None:
            raise RuntimeError("Job lease is not owned by this worker")
        return job

    @staticmethod
    async def current_attempt(session: AsyncSession, job: BackgroundJob) -> JobAttempt:
        attempt = await session.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == job.job_id,
                JobAttempt.attempt_number == job.attempt_count,
            )
        )
        if attempt is None:
            raise RuntimeError("Current job attempt is missing")
        return attempt

    @staticmethod
    def emit(
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        event = OutboxEvent(
            library_id=library_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            event_version=1,
            payload=payload,
            status="PENDING",
            attempts=0,
            available_at=datetime.now(UTC),
        )
        session.add(event)
        return event


job_service = JobService()
