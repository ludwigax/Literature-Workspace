from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update

from backend.app.assets.storage import get_object_storage
from backend.app.audit import record_audit_event
from backend.app.authorization.dependencies import (
    AdminActor,
    CsrfProtected,
    CurrentActor,
    Database,
)
from backend.app.authorization.system_roles import system_role_service
from backend.app.config import get_settings
from backend.app.documents.embeddings import openai_embedding_client
from backend.app.documents.orchestration import document_build_orchestrator
from backend.app.documents.retrieval import EvidenceDatabaseSpec, document_retrieval_service
from backend.app.documents.service import document_domain_service
from backend.app.ingestion.providers import normalize_doi
from backend.app.models import (
    Blob,
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    DocumentBuildRun,
    DocumentBuildTask,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabasePaperScope,
    DocumentDatabaseRelease,
    DocumentPipeline,
    DocumentPipelineVersion,
    PipelineDocument,
)

router = APIRouter(tags=["documents"])
admin_router = APIRouter(prefix="/admin", tags=["administration"])


class PipelineVersionBody(BaseModel):
    system_prompt: str = Field(default="", max_length=500_000)
    user_prompt: str = Field(default="", max_length=500_000)
    model: str = Field(default="", max_length=200)
    splitter_type: Literal["WHOLE", "JSON", "PARAGRAPH", "MARKDOWN", "ADVANCED"]
    splitter_config: dict[str, Any] = Field(default_factory=dict)
    model_config_data: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    input_config: dict[str, Any] = Field(
        default_factory=lambda: {
            "source": "canonical_pdf_text",
            "execution_mode": "LLM",
        }
    )

    @model_validator(mode="after")
    def validate_execution(self) -> PipelineVersionBody:
        mode = str(self.input_config.get("execution_mode") or "LLM").upper()
        if mode not in {"DIRECT_TEXT", "LLM"}:
            raise ValueError("input_config.execution_mode must be DIRECT_TEXT or LLM")
        if mode == "LLM" and (not self.user_prompt.strip() or not self.model.strip()):
            raise ValueError("LLM Pipelines require user_prompt and model")
        return self


class CreatePipelineBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    initial_version: PipelineVersionBody


class UpdatePipelineBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


class CreateDatabaseBody(BaseModel):
    pipeline_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    range_mode: Literal["EXPLICIT", "ALL_VERIFIED"] = "EXPLICIT"
    embedding_profile: dict[str, Any] | None = None
    bm25_profile: dict[str, Any] = Field(default_factory=dict)


class UpdateDatabaseBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    status: Literal["ACTIVE", "ARCHIVED"] | None = None
    range_mode: Literal["EXPLICIT", "ALL_VERIFIED"] | None = None
    embedding_profile: dict[str, Any] | None = None
    bm25_profile: dict[str, Any] | None = None


class ScopeBody(BaseModel):
    canonical_paper_ids: list[uuid.UUID] = Field(max_length=100_000)


class ReconcileBody(BaseModel):
    build_mode: Literal["FULL", "UPDATE"] = "UPDATE"


class ReconcilePolicyBody(BaseModel):
    enabled: bool


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    mode: Literal["BM25", "VECTOR", "HYBRID"] = "HYBRID"
    limit: int = Field(default=20, ge=1, le=200)
    facet_1: str | None = Field(default=None, max_length=500)
    facet_2: str | None = Field(default=None, max_length=500)


class EvidenceDatabaseBody(BaseModel):
    database_id: uuid.UUID
    top_k: int | None = Field(default=None, ge=1, le=100)
    weight: float = Field(default=1.0, gt=0, le=100)


class EvidenceSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    databases: list[EvidenceDatabaseBody] = Field(min_length=1, max_length=20)
    mode: Literal["BM25", "VECTOR", "HYBRID"] = "HYBRID"
    aggregation: Literal["MAX", "INTEGRATE"] = "MAX"
    database_top_k: int = Field(default=20, ge=1, le=100)
    total_top_k: int = Field(default=20, ge=1, le=200)
    chunk_top_k_per_document: int = Field(default=5, ge=1, le=20)
    integrate_decay: float = Field(default=0.5, gt=0, le=1)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    facet_1: str | None = Field(default=None, max_length=500)
    facet_2: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_databases(self) -> EvidenceSearchBody:
        database_ids = [value.database_id for value in self.databases]
        if len(database_ids) != len(set(database_ids)):
            raise ValueError("Each Document Database may only appear once")
        return self


class SystemRoleBody(BaseModel):
    system_role: Literal["ADMIN", "USER"]


class PdfVerificationBody(BaseModel):
    verification_status: Literal["UNVERIFIED", "VERIFIED"]


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _pipeline_view(value: DocumentPipeline) -> dict[str, object]:
    return {
        "pipeline_id": str(value.pipeline_id),
        "name": value.name,
        "description": value.description,
        "status": value.status,
        "active_version_id": str(value.active_version_id) if value.active_version_id else None,
        "created_by": str(value.created_by) if value.created_by else None,
        "created_at": _time(value.created_at),
        "updated_at": _time(value.updated_at),
    }


def _version_view(value: DocumentPipelineVersion) -> dict[str, object]:
    return {
        "pipeline_version_id": str(value.pipeline_version_id),
        "pipeline_id": str(value.pipeline_id),
        "version": value.version,
        "system_prompt": value.system_prompt,
        "user_prompt": value.user_prompt,
        "model": value.model,
        "model_config": value.model_config,
        "input_config": value.input_config,
        "splitter_type": value.splitter_type,
        "splitter_config": value.splitter_config,
        "config_hash": value.config_hash,
        "created_by": str(value.created_by) if value.created_by else None,
        "created_at": _time(value.created_at),
    }


def _database_view(value: DocumentDatabase) -> dict[str, object]:
    return {
        "database_id": str(value.database_id),
        "pipeline_id": str(value.pipeline_id),
        "name": value.name,
        "description": value.description,
        "status": value.status,
        "range_mode": value.range_mode,
        "range_revision": value.range_revision,
        "current_release_id": str(value.current_release_id) if value.current_release_id else None,
        "building_release_id": (
            str(value.building_release_id) if value.building_release_id else None
        ),
        "embedding_profile": value.embedding_profile,
        "bm25_profile": value.bm25_profile,
        "retrieval_status": value.retrieval_status,
        "auto_reconcile_enabled": value.auto_reconcile_enabled,
        "last_reconcile_checked_at": _time(value.last_reconcile_checked_at),
        "next_reconcile_at": _time(value.next_reconcile_at),
        "created_at": _time(value.created_at),
        "updated_at": _time(value.updated_at),
    }


def _release_view(value: DocumentDatabaseRelease) -> dict[str, object]:
    return {
        "release_id": str(value.release_id),
        "database_id": str(value.database_id),
        "release_number": value.release_number,
        "pipeline_version_id": str(value.pipeline_version_id),
        "range_revision": value.range_revision,
        "build_mode": value.build_mode,
        "trigger_reason": value.trigger_reason,
        "status": value.status,
        "expected_count": value.expected_count,
        "completed_count": value.completed_count,
        "failed_count": value.failed_count,
        "retrieval_status": value.retrieval_status,
        "published_at": _time(value.published_at),
        "archived_at": _time(value.archived_at),
        "created_at": _time(value.created_at),
    }


def _run_view(value: DocumentBuildRun) -> dict[str, object]:
    return {
        "run_id": str(value.run_id),
        "database_id": str(value.database_id),
        "release_id": str(value.release_id) if value.release_id else None,
        "pipeline_version_id": str(value.pipeline_version_id),
        "range_revision": value.range_revision,
        "build_mode": value.build_mode,
        "trigger_reason": value.trigger_reason,
        "status": value.status,
        "phase": value.phase,
        "reconcile_requested": value.reconcile_requested,
        "result": value.result,
        "error": value.error,
        "created_at": _time(value.created_at),
        "updated_at": _time(value.updated_at),
        "finished_at": _time(value.finished_at),
    }


def _task_view(value: DocumentBuildTask) -> dict[str, object]:
    return {
        "task_id": str(value.task_id),
        "task_type": value.task_type,
        "queue_name": value.queue_name,
        "subject_key": value.subject_key,
        "status": value.status,
        "progress_current": value.progress_current,
        "progress_total": value.progress_total,
        "progress_message": value.progress_message,
        "attempt_count": value.attempt_count,
        "max_attempts": value.max_attempts,
        "result": value.result,
        "error": value.error,
        "created_at": _time(value.created_at),
        "updated_at": _time(value.updated_at),
    }


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))


@router.get("/document-pipelines")
async def list_pipelines(session: Database, _: CurrentActor) -> dict[str, object]:
    values = list(
        await session.scalars(
            select(DocumentPipeline).order_by(DocumentPipeline.name, DocumentPipeline.pipeline_id)
        )
    )
    return {"pipelines": [_pipeline_view(value) for value in values]}


@router.get("/document-pipelines/{pipeline_id}")
async def get_pipeline(
    pipeline_id: uuid.UUID, session: Database, _: CurrentActor
) -> dict[str, object]:
    value = await session.get(DocumentPipeline, pipeline_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return _pipeline_view(value)


@router.get("/document-pipelines/{pipeline_id}/versions")
async def list_pipeline_versions(
    pipeline_id: uuid.UUID, session: Database, _: CurrentActor
) -> dict[str, object]:
    if await session.get(DocumentPipeline, pipeline_id) is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    values = list(
        await session.scalars(
            select(DocumentPipelineVersion)
            .where(DocumentPipelineVersion.pipeline_id == pipeline_id)
            .order_by(DocumentPipelineVersion.version.desc())
        )
    )
    return {"versions": [_version_view(value) for value in values]}


@router.post("/document-pipelines", status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    body: CreatePipelineBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        pipeline = await document_domain_service.create_pipeline(
            session,
            name=body.name,
            description=body.description,
            created_by=actor.principal_id,
        )
        version = await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            **body.initial_version.model_dump(by_alias=True),
            created_by=actor.principal_id,
        )
        record_audit_event(
            session,
            "document.pipeline_created",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"pipeline_id": str(pipeline.pipeline_id)},
        )
        await session.commit()
        await session.refresh(pipeline)
        await session.refresh(version)
        return {"pipeline": _pipeline_view(pipeline), "active_version": _version_view(version)}
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.patch("/document-pipelines/{pipeline_id}")
async def update_pipeline(
    pipeline_id: uuid.UUID,
    body: UpdatePipelineBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    value = await session.get(DocumentPipeline, pipeline_id, with_for_update=True)
    if value is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    if body.name is not None:
        value.name = body.name.strip()
    if body.description is not None:
        value.description = body.description.strip()
    if body.status is not None:
        value.status = body.status
    record_audit_event(
        session,
        "document.pipeline_updated",
        actor_principal_id=actor.principal_id,
        session_id=actor.session_id,
        details={"pipeline_id": str(pipeline_id)},
    )
    await session.commit()
    await session.refresh(value)
    return _pipeline_view(value)


@router.post("/document-pipelines/{pipeline_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_pipeline_version(
    pipeline_id: uuid.UUID,
    body: PipelineVersionBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    previous_max = int(
        await session.scalar(
            select(func.coalesce(func.max(DocumentPipelineVersion.version), 0)).where(
                DocumentPipelineVersion.pipeline_id == pipeline_id
            )
        )
        or 0
    )
    try:
        value = await document_domain_service.add_pipeline_version(
            session,
            pipeline_id,
            **body.model_dump(by_alias=True),
            created_by=actor.principal_id,
        )
        created = value.version > previous_max
        record_audit_event(
            session,
            "document.pipeline_version_activated",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={
                "pipeline_id": str(pipeline_id),
                "pipeline_version_id": str(value.pipeline_version_id),
                "created": created,
            },
        )
        await session.commit()
        await session.refresh(value)
        return {"version": _version_view(value), "created": created}
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.get("/document-databases")
async def list_databases(session: Database, _: CurrentActor) -> dict[str, object]:
    values = list(
        await session.scalars(
            select(DocumentDatabase).order_by(DocumentDatabase.name, DocumentDatabase.database_id)
        )
    )
    return {"databases": [_database_view(value) for value in values]}


@router.get("/document-databases/{database_id}")
async def get_database(
    database_id: uuid.UUID, session: Database, _: CurrentActor
) -> dict[str, object]:
    value = await session.get(DocumentDatabase, database_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Document Database not found")
    return _database_view(value)


@router.post("/document-databases", status_code=status.HTTP_201_CREATED)
async def create_database(
    body: CreateDatabaseBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        values = body.model_dump()
        if values["embedding_profile"] is None:
            settings = get_settings()
            values["embedding_profile"] = {
                "provider": "OPENAI_COMPATIBLE",
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "batch_size": settings.embedding_batch_size,
                "max_batch_tokens": settings.embedding_max_batch_tokens,
            }
        value = await document_domain_service.create_database(
            session, **values, created_by=actor.principal_id
        )
        record_audit_event(
            session,
            "document.database_created",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"database_id": str(value.database_id)},
        )
        await session.commit()
        await session.refresh(value)
        return _database_view(value)
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.patch("/document-databases/{database_id}")
async def update_database(
    database_id: uuid.UUID,
    body: UpdateDatabaseBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    value = await session.get(DocumentDatabase, database_id, with_for_update=True)
    if value is None:
        raise HTTPException(status_code=404, detail="Document Database not found")
    range_changed = False
    if body.range_mode is not None:
        value, range_changed = await document_domain_service.set_range_mode(
            session, database_id, body.range_mode
        )
    if body.name is not None:
        value.name = body.name.strip()
    if body.description is not None:
        value.description = body.description.strip()
    if body.status is not None:
        value.status = body.status
    profiles_changed = False
    if body.embedding_profile is not None:
        profiles_changed |= value.embedding_profile != body.embedding_profile
        value.embedding_profile = body.embedding_profile
    if body.bm25_profile is not None:
        profiles_changed |= value.bm25_profile != body.bm25_profile
        value.bm25_profile = body.bm25_profile
    if profiles_changed:
        await session.execute(
            update(DocumentBuildRun)
            .where(
                DocumentBuildRun.database_id == database_id,
                DocumentBuildRun.status == "RUNNING",
            )
            .values(reconcile_requested=True)
        )
    record_audit_event(
        session,
        "document.database_updated",
        actor_principal_id=actor.principal_id,
        session_id=actor.session_id,
        details={"database_id": str(database_id), "range_changed": range_changed},
    )
    await session.commit()
    await session.refresh(value)
    return _database_view(value)


@router.get("/document-databases/{database_id}/scope")
async def get_database_scope(
    database_id: uuid.UUID, session: Database, _: CurrentActor
) -> dict[str, object]:
    database = await session.get(DocumentDatabase, database_id)
    if database is None:
        raise HTTPException(status_code=404, detail="Document Database not found")
    explicit_paper_ids = list(
        await session.scalars(
            select(DocumentDatabasePaperScope.canonical_paper_id)
            .where(DocumentDatabasePaperScope.database_id == database_id)
            .order_by(DocumentDatabasePaperScope.canonical_paper_id)
        )
    )
    resolved_paper_ids = await document_domain_service.resolve_scope(session, database)
    return {
        "database_id": str(database_id),
        "range_mode": database.range_mode,
        "range_revision": database.range_revision,
        "canonical_paper_ids": [str(value) for value in resolved_paper_ids],
        "explicit_canonical_paper_ids": [str(value) for value in explicit_paper_ids],
    }


@router.put("/document-databases/{database_id}/scope")
async def replace_database_scope(
    database_id: uuid.UUID,
    body: ScopeBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        changed = await document_domain_service.replace_explicit_scope(
            session,
            database_id,
            set(body.canonical_paper_ids),
            actor_principal_id=actor.principal_id,
        )
        record_audit_event(
            session,
            "document.database_scope_replaced",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"database_id": str(database_id), "changed": changed},
        )
        await session.commit()
        return {"database_id": str(database_id), "changed": changed}
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.patch("/document-databases/{database_id}/reconcile-policy")
async def set_reconcile_policy(
    database_id: uuid.UUID,
    body: ReconcilePolicyBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await document_build_orchestrator.set_reconcile_policy(
            session, database_id, enabled=body.enabled
        )
        record_audit_event(
            session,
            "document.reconcile_policy_updated",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"database_id": str(database_id), "enabled": body.enabled},
        )
        await session.commit()
        await session.refresh(value)
        return _database_view(value)
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.post("/document-databases/{database_id}/reconcile", status_code=status.HTTP_202_ACCEPTED)
async def reconcile_database(
    database_id: uuid.UUID,
    body: ReconcileBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        run = await document_build_orchestrator.start_build(
            session,
            database_id,
            build_mode=body.build_mode,
            trigger_reason="MANUAL",
            actor_principal_id=actor.principal_id,
            defer_advance=True,
        )
        record_audit_event(
            session,
            "document.reconcile_requested",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"database_id": str(database_id), "run_id": str(run.run_id)},
        )
        await session.commit()
        await session.refresh(run)
        return _run_view(run)
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.get("/document-databases/{database_id}/releases")
async def list_releases(
    database_id: uuid.UUID, session: Database, _: CurrentActor
) -> dict[str, object]:
    if await session.get(DocumentDatabase, database_id) is None:
        raise HTTPException(status_code=404, detail="Document Database not found")
    values = list(
        await session.scalars(
            select(DocumentDatabaseRelease)
            .where(DocumentDatabaseRelease.database_id == database_id)
            .order_by(DocumentDatabaseRelease.release_number.desc())
        )
    )
    return {"releases": [_release_view(value) for value in values]}


@router.get("/document-build-runs")
async def list_build_runs(
    session: Database,
    _: CurrentActor,
    database_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    statement = select(DocumentBuildRun).order_by(
        DocumentBuildRun.created_at.desc(), DocumentBuildRun.run_id
    )
    if database_id is not None:
        statement = statement.where(DocumentBuildRun.database_id == database_id)
    values = list(await session.scalars(statement.limit(limit)))
    return {"runs": [_run_view(value) for value in values]}


@router.get("/document-build-runs/{run_id}")
async def get_build_run(run_id: uuid.UUID, session: Database, _: CurrentActor) -> dict[str, object]:
    value = await session.get(DocumentBuildRun, run_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Document BuildRun not found")
    tasks = list(
        await session.scalars(
            select(DocumentBuildTask)
            .where(DocumentBuildTask.run_id == run_id)
            .order_by(DocumentBuildTask.created_at, DocumentBuildTask.task_id)
        )
    )
    return {"run": _run_view(value), "tasks": [_task_view(task) for task in tasks]}


@router.post("/document-build-runs/{run_id}/cancel")
async def cancel_build_run(
    run_id: uuid.UUID,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await document_build_orchestrator.cancel(
            session, run_id, actor_principal_id=actor.principal_id
        )
        record_audit_event(
            session,
            "document.build_cancelled",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"run_id": str(run_id)},
        )
        await session.commit()
        await session.refresh(value)
        return _run_view(value)
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.post("/document-build-runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_build_run(
    run_id: uuid.UUID,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await document_build_orchestrator.retry(
            session,
            run_id,
            actor_principal_id=actor.principal_id,
            defer_advance=True,
        )
        record_audit_event(
            session,
            "document.build_retried",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={"old_run_id": str(run_id), "new_run_id": str(value.run_id)},
        )
        await session.commit()
        await session.refresh(value)
        return _run_view(value)
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@router.post("/document-databases/{database_id}/search")
async def search_database(
    database_id: uuid.UUID,
    body: SearchBody,
    session: Database,
    _: CurrentActor,
) -> dict[str, object]:
    try:
        async with httpx.AsyncClient() as http:
            return await document_retrieval_service.search(
                session,
                get_object_storage(),
                openai_embedding_client(http),
                database_id=database_id,
                **body.model_dump(),
            )
    except Exception as error:
        raise _http_error(error) from error


@router.post("/retrieval/search")
async def search_evidence(
    body: EvidenceSearchBody,
    session: Database,
    _: CurrentActor,
) -> dict[str, object]:
    try:
        specs = [
            EvidenceDatabaseSpec(
                database_id=value.database_id,
                top_k=value.top_k or body.database_top_k,
                weight=value.weight,
            )
            for value in body.databases
        ]
        async with httpx.AsyncClient() as http:
            return await document_retrieval_service.search_evidence(
                session,
                get_object_storage(),
                openai_embedding_client(http),
                databases=specs,
                query=body.query,
                mode=body.mode,
                aggregation=body.aggregation,
                total_top_k=body.total_top_k,
                chunk_top_k_per_document=body.chunk_top_k_per_document,
                integrate_decay=body.integrate_decay,
                rrf_k=body.rrf_k,
                facet_1=body.facet_1,
                facet_2=body.facet_2,
            )
    except Exception as error:
        raise _http_error(error) from error


@router.get("/canonical-papers/by-doi")
async def get_canonical_paper_by_doi(
    doi: Annotated[str, Query(min_length=4, max_length=500)],
    session: Database,
    _: CurrentActor,
) -> dict[str, object]:
    try:
        normalized = normalize_doi(doi)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid DOI") from error
    identifier = await session.scalar(
        select(CanonicalIdentifier).where(
            CanonicalIdentifier.scheme == "DOI",
            CanonicalIdentifier.normalized_value == normalized,
        )
    )
    if identifier is None:
        return {"doi": normalized, "status": "NOT_FOUND", "documents": []}
    paper = await session.get(CanonicalPaper, identifier.canonical_paper_id)
    if paper is None or paper.status != "ACTIVE":
        return {"doi": normalized, "status": "NOT_FOUND", "documents": []}
    metadata = await session.get(CanonicalMetadata, paper.canonical_paper_id)
    identifiers = list(
        await session.scalars(
            select(CanonicalIdentifier)
            .where(CanonicalIdentifier.canonical_paper_id == paper.canonical_paper_id)
            .order_by(CanonicalIdentifier.scheme, CanonicalIdentifier.normalized_value)
        )
    )
    documents = list(
        await session.scalars(
            select(PipelineDocument)
            .where(PipelineDocument.canonical_paper_id == paper.canonical_paper_id)
            .order_by(PipelineDocument.created_at.desc(), PipelineDocument.document_id)
        )
    )
    metadata_view: dict[str, object] | None = None
    if metadata is not None:
        metadata_view = {
            "title": metadata.title,
            "abstract": metadata.abstract,
            "publication_year": metadata.publication_year,
            "work_type": metadata.work_type,
            "venue": metadata.venue,
            "canonical_url": metadata.canonical_url,
            "authors": metadata.authors,
        }
    return {
        "doi": normalized,
        "status": "FOUND",
        "canonical_paper_id": str(paper.canonical_paper_id),
        "metadata": metadata_view,
        "identifiers": [
            {"scheme": value.scheme, "value": value.original_value}
            for value in identifiers
        ],
        "documents": [
            {
                "document_id": str(value.document_id),
                "pipeline_version_id": str(value.pipeline_version_id),
                "display_title": value.display_title,
                "media_type": value.media_type,
                "word_count": value.word_count,
                "created_at": value.created_at.isoformat(),
            }
            for value in documents
        ],
    }


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID, session: Database, _: CurrentActor
) -> dict[str, object]:
    value = await session.get(PipelineDocument, document_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Document not found")
    blob = await session.get(Blob, value.content_blob_id)
    if blob is None or blob.status != "AVAILABLE":
        raise HTTPException(status_code=404, detail="Document content is unavailable")
    data = await get_object_storage().read_bytes(blob.storage_key, blob.byte_size + 1)
    chunk_count = int(
        await session.scalar(
            select(func.count(DocumentChunk.chunk_id)).where(
                DocumentChunk.document_id == document_id
            )
        )
        or 0
    )
    return {
        "document_id": str(value.document_id),
        "canonical_paper_id": str(value.canonical_paper_id),
        "pipeline_version_id": str(value.pipeline_version_id),
        "display_title": value.display_title,
        "media_type": value.media_type,
        "content": data.decode("utf-8"),
        "content_sha256": value.content_sha256,
        "word_count": value.word_count,
        "chunk_count": chunk_count,
        "provenance": value.provenance,
    }


@router.get("/chunks/{chunk_id}")
async def get_chunk(chunk_id: uuid.UUID, session: Database, _: CurrentActor) -> dict[str, object]:
    value = await session.get(DocumentChunk, chunk_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return {
        "chunk_id": str(value.chunk_id),
        "document_id": str(value.document_id),
        "canonical_paper_id": str(value.canonical_paper_id),
        "ordinal": value.ordinal,
        "content": value.content,
        "content_sha256": value.content_sha256,
        "word_count": value.word_count,
        "facet_1": value.facet_1,
        "facet_2": value.facet_2,
        "attributes": value.attributes,
    }


@admin_router.get("/principals")
async def list_principals(session: Database, _: AdminActor) -> dict[str, object]:
    return {"principals": await system_role_service.list_principals(session)}


@admin_router.patch("/canonical-papers/{canonical_paper_id}/pdf-verification")
async def update_pdf_verification(
    canonical_paper_id: uuid.UUID,
    body: PdfVerificationBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        artifact, changed = await document_domain_service.set_pdf_verification(
            session,
            canonical_paper_id,
            body.verification_status,
            actor_principal_id=actor.principal_id,
        )
        record_audit_event(
            session,
            "canonical_pdf.verification_changed",
            actor_principal_id=actor.principal_id,
            session_id=actor.session_id,
            details={
                "canonical_paper_id": str(canonical_paper_id),
                "verification_status": artifact.verification_status,
                "changed": changed,
            },
        )
        await session.commit()
        await session.refresh(artifact)
        return {
            "canonical_paper_id": str(canonical_paper_id),
            "artifact_id": str(artifact.artifact_id),
            "verification_status": artifact.verification_status,
            "artifact_revision": artifact.revision,
            "changed": changed,
        }
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error


@admin_router.patch("/principals/{principal_id}/system-role")
async def update_system_role(
    principal_id: uuid.UUID,
    body: SystemRoleBody,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await system_role_service.assign(
            session, actor, principal_id, role=body.system_role
        )
        await session.commit()
        return value
    except Exception as error:
        await session.rollback()
        raise _http_error(error) from error
