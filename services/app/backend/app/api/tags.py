from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from ..authorization.dependencies import CsrfProtected, CurrentActor, Database
from ..tags.service import tag_service

router = APIRouter(prefix="/libraries", tags=["tags"])


class TagBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class UpdateTagBody(TagBody):
    expected_revision: int = Field(ge=1)


@router.get("/{library_id}/tags")
async def list_tags(
    library_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    return {"tags": await tag_service.list(session, actor, library_id)}


@router.post("/{library_id}/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(
    library_id: uuid.UUID,
    body: TagBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await tag_service.create(session, actor, library_id, name=body.name, color=body.color)


@router.patch("/{library_id}/tags/{tag_id}")
async def update_tag(
    library_id: uuid.UUID,
    tag_id: uuid.UUID,
    body: UpdateTagBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await tag_service.update(
        session,
        actor,
        library_id,
        tag_id,
        name=body.name,
        color=body.color,
        expected_revision=body.expected_revision,
    )


@router.delete("/{library_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    library_id: uuid.UUID,
    tag_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    expected_revision: int = Query(ge=1),
) -> Response:
    await tag_service.remove(
        session, actor, library_id, tag_id, expected_revision=expected_revision
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
