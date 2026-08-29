from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import BackgroundJob


class JobHandler(Protocol):
    job_type: str

    async def handle(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> None: ...


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, handler: JobHandler) -> None:
        if handler.job_type in self._handlers:
            raise ValueError(f"Job handler already registered: {handler.job_type}")
        self._handlers[handler.job_type] = handler

    def require(self, job_type: str) -> JobHandler:
        try:
            return self._handlers[job_type]
        except KeyError as error:
            raise LookupError(f"No handler registered for {job_type}") from error

    @property
    def job_types(self) -> set[str]:
        return set(self._handlers)
