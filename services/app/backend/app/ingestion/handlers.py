from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import BackgroundJob

from .providers import MetadataResolver
from .service import METADATA_REFRESH_JOB, metadata_refresh_service


class MetadataRefreshHandler:
    job_type = METADATA_REFRESH_JOB

    def __init__(self, resolver: MetadataResolver) -> None:
        self.resolver = resolver

    async def handle(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> None:
        await metadata_refresh_service.execute_claimed(
            session,
            job,
            worker_id=worker_id,
            resolver=self.resolver,
        )
