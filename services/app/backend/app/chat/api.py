from __future__ import annotations

import uuid
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..authorization.dependencies import AdminActor, CsrfProtected, CurrentActor, Database
from ..config import get_settings
from ..database import session_factory as api_session_factory
from .service import ChatService, ConflictError, NotFoundError, chat_service
from .sse import stream_turn_events
from .tool_config import (
    LiteratureToolConfig,
    get_literature_tool_config,
    update_literature_tool_config,
)

router = APIRouter()


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class TurnCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    branch_id: uuid.UUID | None = None
    parent_unit_id: uuid.UUID | None = None
    base_revision: int | None = Field(default=None, ge=0)
    max_tool_calls: int = Field(default=0, ge=0)


class RegenerateRequest(BaseModel):
    base_revision: int | None = Field(default=None, ge=0)
    max_tool_calls: int = Field(default=0, ge=0)


class EditAndRegenerateRequest(RegenerateRequest):
    content: str = Field(min_length=1, max_length=100_000)


class LiteratureToolConfigUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    retrieval_mode: Literal["BM25", "VECTOR", "HYBRID"]
    retrieval_top_k: int = Field(ge=1, le=100)
    chunk_top_k_per_document: int = Field(ge=1, le=20)
    doi_document_max_chars: int = Field(ge=1_000, le=100_000)


def _tool_config_view(config: LiteratureToolConfig, revision: int) -> dict[str, object]:
    return {
        "tool_config": {
            "retrieval_mode": config.retrieval_mode,
            "retrieval_top_k": config.retrieval_top_k,
            "chunk_top_k_per_document": config.chunk_top_k_per_document,
            "doi_document_max_chars": config.doi_document_max_chars,
            "revision": revision,
        }
    }


def _raise(error: Exception) -> NoReturn:
    if isinstance(error, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if isinstance(error, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    raise error


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: Database) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/admin/tool-config")
async def get_tool_config(session: Database, _: AdminActor) -> dict[str, object]:
    config, revision = await get_literature_tool_config(session, get_settings())
    return _tool_config_view(config, revision)


@router.patch("/admin/tool-config")
async def update_tool_config(
    body: LiteratureToolConfigUpdate,
    session: Database,
    actor: AdminActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        config, revision = await update_literature_tool_config(
            session,
            get_settings(),
            expected_revision=body.expected_revision,
            values=body.model_dump(exclude={"expected_revision"}),
            updated_by=actor.principal_id,
        )
        return _tool_config_view(config, revision)
    except Exception as error:
        _raise(error)


@router.post("/sessions", status_code=201)
async def create_session(
    body: SessionCreate, session: Database, actor: CurrentActor, _: CsrfProtected
) -> dict[str, object]:
    try:
        value = await chat_service.create_session(
            session, owner_principal_id=actor.principal_id, title=body.title
        )
        return {"session": value}
    except Exception as error:
        _raise(error)


@router.get("/sessions")
async def list_sessions(session: Database, actor: CurrentActor) -> dict[str, object]:
    values = await chat_service.list_sessions(
        session, owner_principal_id=actor.principal_id
    )
    return {"sessions": values}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    try:
        value = await chat_service.get_session(
            session,
            owner_principal_id=actor.principal_id,
            session_id=session_id,
        )
        return {"session": value}
    except Exception as error:
        _raise(error)


@router.get("/sessions/{session_id}/graph")
async def get_graph(
    session_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    try:
        return await chat_service.graph(
            session,
            owner_principal_id=actor.principal_id,
            session_id=session_id,
        )
    except Exception as error:
        _raise(error)


@router.post("/sessions/{session_id}/turns", status_code=202)
async def create_turn(
    session_id: uuid.UUID,
    body: TurnCreate,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await chat_service.create_turn(
            session,
            owner_principal_id=actor.principal_id,
            session_id=session_id,
            content=body.content,
            branch_id=body.branch_id,
            parent_unit_id=body.parent_unit_id,
            base_revision=body.base_revision,
            max_tool_calls=body.max_tool_calls,
        )
        return {"turn": value}
    except Exception as error:
        _raise(error)


@router.post("/units/{unit_id}/regenerate", status_code=202)
async def regenerate(
    unit_id: uuid.UUID,
    body: RegenerateRequest,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await chat_service.regenerate(
            session,
            owner_principal_id=actor.principal_id,
            unit_id=unit_id,
            base_revision=body.base_revision,
            max_tool_calls=body.max_tool_calls,
        )
        return {"turn": value}
    except Exception as error:
        _raise(error)


@router.post("/units/{unit_id}/edit-and-regenerate", status_code=202)
async def edit_and_regenerate(
    unit_id: uuid.UUID,
    body: EditAndRegenerateRequest,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        value = await chat_service.edit_and_regenerate(
            session,
            owner_principal_id=actor.principal_id,
            unit_id=unit_id,
            content=body.content,
            base_revision=body.base_revision,
            max_tool_calls=body.max_tool_calls,
        )
        return {"turn": value}
    except Exception as error:
        _raise(error)


@router.get("/turns/{turn_id}")
async def get_turn(
    turn_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    try:
        value = await chat_service.get_turn(
            session, owner_principal_id=actor.principal_id, turn_id=turn_id
        )
        return {"turn": value}
    except Exception as error:
        _raise(error)


@router.post("/turns/{turn_id}/interrupt", status_code=202)
async def interrupt_turn(
    turn_id: uuid.UUID, session: Database, actor: CurrentActor, _: CsrfProtected
) -> dict[str, object]:
    try:
        value = await chat_service.request_interrupt(
            session, owner_principal_id=actor.principal_id, turn_id=turn_id
        )
        return {"turn": value}
    except Exception as error:
        _raise(error)


@router.get("/turns/{turn_id}/events")
async def get_events(
    turn_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    after: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    try:
        values = await chat_service.get_events(
            session,
            owner_principal_id=actor.principal_id,
            turn_id=turn_id,
            after=after,
        )
        return {"events": values}
    except Exception as error:
        _raise(error)


@router.get("/turns/{turn_id}/tool-executions")
async def get_tool_executions(
    turn_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    try:
        values = await chat_service.get_tool_executions(
            session, owner_principal_id=actor.principal_id, turn_id=turn_id
        )
        return {"tool_executions": values}
    except Exception as error:
        _raise(error)


@router.get("/turns/{turn_id}/events/stream")
async def stream_events(
    turn_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        await chat_service.get_turn(
            session, owner_principal_id=actor.principal_id, turn_id=turn_id
        )
        header_cursor = 0
        if last_event_id is not None:
            header_cursor = int(last_event_id)
            if header_cursor < 0:
                raise ValueError("Last-Event-ID must be non-negative")
        cursor = max(after or 0, header_cursor)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Last-Event-ID must be a non-negative integer",
        ) from error
    except Exception as error:
        _raise(error)

    settings = get_settings()
    return StreamingResponse(
        stream_turn_events(
            session_factory=api_session_factory,
            owner_principal_id=actor.principal_id,
            turn_id=turn_id,
            after=cursor,
            poll_seconds=settings.chat_sse_poll_seconds,
            heartbeat_seconds=settings.chat_sse_heartbeat_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["ChatService", "router"]
