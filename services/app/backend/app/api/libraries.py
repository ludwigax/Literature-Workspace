from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from ..authorization.dependencies import CsrfProtected, CurrentActor, Database
from ..config import get_settings
from ..libraries.service import library_service

router = APIRouter(prefix="/libraries", tags=["libraries"])
invitation_router = APIRouter(prefix="/library-invitations", tags=["libraries"])


class CreateLibraryBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class InvitationBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str


class AcceptInvitationBody(BaseModel):
    token: str = Field(min_length=16, max_length=500)


class MemberRoleBody(BaseModel):
    role: str


@router.get("")
async def list_libraries(session: Database, actor: CurrentActor) -> dict[str, object]:
    return {"libraries": await library_service.list_for_actor(session, actor)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group_library(
    body: CreateLibraryBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await library_service.create_group(session, actor, name=body.name)


@router.get("/{library_id}")
async def get_library(
    library_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    return await library_service.get_for_actor(session, actor, library_id)


@router.get("/{library_id}/members")
async def list_members(
    library_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    return {"members": await library_service.list_members(session, actor, library_id)}


@router.patch("/{library_id}/members/{principal_id}")
async def update_member_role(
    library_id: uuid.UUID,
    principal_id: uuid.UUID,
    body: MemberRoleBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await library_service.update_member_role(
        session, actor, library_id, principal_id, role=body.role
    )


@router.delete("/{library_id}/members/{principal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    library_id: uuid.UUID,
    principal_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> Response:
    await library_service.remove_member(session, actor, library_id, principal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{library_id}/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    library_id: uuid.UUID,
    body: InvitationBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    settings = get_settings()
    if settings.env == "production":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="invitation delivery provider is not configured",
        )
    created = await library_service.create_invitation(
        session,
        actor,
        library_id,
        email=body.email,
        role=body.role,
    )
    return {
        **library_service.invitation_view(created.invitation),
        "accept_token": created.accept_token,
    }


@router.get("/{library_id}/invitations")
async def list_invitations(
    library_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    return {"invitations": await library_service.list_invitations(session, actor, library_id)}


@router.post("/{library_id}/invitations/{invitation_id}/regenerate")
async def regenerate_invitation(
    library_id: uuid.UUID,
    invitation_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    if get_settings().env == "production":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="invitation delivery provider is not configured",
        )
    created = await library_service.regenerate_invitation(
        session,
        actor,
        library_id,
        invitation_id,
    )
    return {
        **library_service.invitation_view(created.invitation),
        "accept_token": created.accept_token,
    }


@invitation_router.post("/accept")
async def accept_invitation(
    body: AcceptInvitationBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await library_service.accept_invitation(session, actor, token=body.token)
