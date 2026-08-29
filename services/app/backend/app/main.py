from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.auth import router as auth_router
from .api.catalogue import router as catalogue_router
from .api.documents import admin_router as document_admin_router
from .api.documents import router as documents_router
from .api.fake_pipeline import router as fake_pipeline_router
from .api.health import router as health_router
from .api.ingestion import router as ingestion_router
from .api.libraries import invitation_router
from .api.libraries import router as libraries_router
from .api.resources import router as resources_router
from .api.tags import router as tags_router
from .config import get_settings
from .database import engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="Literature Workspace v2 Library API",
        version="0.1.0",
        docs_url="/api/v2/docs" if settings.env != "production" else None,
        openapi_url="/api/v2/openapi.json",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url.rstrip("/")],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "If-Match"],
    )
    application.include_router(health_router, prefix="/api/v2")
    application.include_router(fake_pipeline_router, prefix="/api/v2")
    application.include_router(auth_router, prefix="/api/v2")
    application.include_router(libraries_router, prefix="/api/v2")
    application.include_router(invitation_router, prefix="/api/v2")
    application.include_router(catalogue_router, prefix="/api/v2")
    application.include_router(ingestion_router, prefix="/api/v2")
    application.include_router(resources_router, prefix="/api/v2")
    application.include_router(tags_router, prefix="/api/v2")
    application.include_router(documents_router, prefix="/api/v2")
    application.include_router(document_admin_router, prefix="/api/v2")
    return application


app = create_app()
