from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


settings = get_settings()
api_engine = build_engine(settings.database_url)
worker_engine = build_engine(settings.worker_database_url)
api_session_factory = async_sessionmaker(api_engine, expire_on_commit=False)
worker_session_factory = async_sessionmaker(worker_engine, expire_on_commit=False)


async def database_session() -> AsyncIterator[AsyncSession]:
    async with api_session_factory() as session:
        yield session
