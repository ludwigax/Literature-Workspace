from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    item_id: uuid.UUID
    item_type: str


class LeaseWorkerBackend(Protocol):
    async def claim(self, session: AsyncSession, *, worker_id: str) -> LeaseClaim | None: ...

    async def execute(
        self, session: AsyncSession, claim: LeaseClaim, *, worker_id: str
    ) -> None: ...

    async def fail_unexpected(
        self,
        session: AsyncSession,
        claim: LeaseClaim,
        *,
        worker_id: str,
        error: Exception,
    ) -> None: ...


class LeaseWorker:
    """Shared transaction boundary for independently stored leased queues."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        backend: LeaseWorkerBackend,
        *,
        idle_seconds: float = 1.0,
    ) -> None:
        self.session_factory = session_factory
        self.backend = backend
        self.idle_seconds = idle_seconds

    async def run_once(self, worker_id: str) -> bool:
        async with self.session_factory() as session:
            claim = await self.backend.claim(session, worker_id=worker_id)
            if claim is None:
                await session.rollback()
                return False
            await session.commit()

        async with self.session_factory() as session:
            try:
                await self.backend.execute(session, claim, worker_id=worker_id)
            except Exception as error:
                logger.exception(
                    "Work item %s (%s) failed unexpectedly", claim.item_id, claim.item_type
                )
                await session.rollback()
                async with self.session_factory() as failure_session:
                    await self.backend.fail_unexpected(
                        failure_session,
                        claim,
                        worker_id=worker_id,
                        error=error,
                    )
                    await failure_session.commit()
                return True
            await session.commit()
            return True

    async def run_slot(self, worker_id: str) -> None:
        while True:
            worked = await self.run_once(worker_id)
            if not worked:
                await asyncio.sleep(self.idle_seconds)
