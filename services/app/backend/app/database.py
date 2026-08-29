from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings


def build_engine(database_url: str | None = None) -> AsyncEngine:
    url = database_url or get_settings().database_url
    return create_async_engine(url, pool_pre_ping=True)


engine = build_engine()
session_factory = async_sessionmaker(engine, expire_on_commit=False)
migration_engine = build_engine(get_settings().migration_database_url)
migration_session_factory = async_sessionmaker(migration_engine, expire_on_commit=False)
worker_engine = build_engine(get_settings().worker_database_url)
worker_session_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
chat_worker_engine = build_engine(get_settings().chat_worker_database_url)
chat_worker_session_factory = async_sessionmaker(chat_worker_engine, expire_on_commit=False)


async def database_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
