from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import router
from .config import get_settings
from .database import api_engine, worker_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await api_engine.dispose()
    await worker_engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="Chat Workspace v2 API",
        version="0.1.0",
        docs_url="/api/chat/v1/docs" if settings.env != "production" else None,
        openapi_url="/api/chat/v1/openapi.json",
        lifespan=lifespan,
    )
    application.include_router(router, prefix="/api/chat/v1")
    return application


app = create_app()
