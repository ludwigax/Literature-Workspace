from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import database_session
from ..identity.security import hash_token
from ..models import LibraryMembership, Principal, PrincipalSystemRole, WebSession

Database = Annotated[AsyncSession, Depends(database_session)]


@dataclass(frozen=True)
class Actor:
    principal_id: uuid.UUID
    display_name: str
    session_id: uuid.UUID
    system_role: str = "USER"


async def current_actor(request: Request, session: Database) -> Actor:
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name, "")
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    row = (
        await session.execute(
            select(WebSession, Principal, PrincipalSystemRole.role)
            .join(Principal, Principal.principal_id == WebSession.principal_id)
            .outerjoin(
                PrincipalSystemRole,
                PrincipalSystemRole.principal_id == Principal.principal_id,
            )
            .where(
                WebSession.token_hash == hash_token(raw_token),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > datetime.now(UTC),
                Principal.status == "ACTIVE",
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session is invalid")
    web_session, principal, system_role = row
    await session.execute(
        text("SELECT set_config('app.principal_id', :principal_id, true)"),
        {"principal_id": str(principal.principal_id)},
    )
    return Actor(
        principal_id=principal.principal_id,
        display_name=principal.display_name,
        session_id=web_session.session_id,
        system_role=str(system_role or "USER"),
    )


CurrentActor = Annotated[Actor, Depends(current_actor)]


async def require_admin(actor: CurrentActor) -> Actor:
    if actor.system_role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return actor


AdminActor = Annotated[Actor, Depends(require_admin)]


async def require_csrf(
    request: Request,
    session: Database,
    actor: CurrentActor,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> None:
    settings = get_settings()
    cookie_token = request.cookies.get(settings.csrf_cookie_name, "")
    header_token = str(x_csrf_token or "")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    stored_hash = await session.scalar(
        select(WebSession.csrf_token_hash).where(WebSession.session_id == actor.session_id)
    )
    if stored_hash is None or not hmac.compare_digest(stored_hash, hash_token(header_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


CsrfProtected = Annotated[None, Depends(require_csrf)]


async def membership_for(
    session: AsyncSession,
    *,
    actor: Actor,
    library_id: uuid.UUID,
    allowed_roles: set[str] | None = None,
) -> LibraryMembership:
    membership = await session.scalar(
        select(LibraryMembership).where(
            LibraryMembership.library_id == library_id,
            LibraryMembership.principal_id == actor.principal_id,
            LibraryMembership.status == "ACTIVE",
        )
    )
    if membership is None or (allowed_roles is not None and membership.role not in allowed_roles):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="library not found")
    return membership
