from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, status

from .config import get_settings


@dataclass(frozen=True)
class Actor:
    principal_id: uuid.UUID


async def current_actor(
    x_chat_principal_id: Annotated[str | None, Header()] = None,
) -> Actor:
    settings = get_settings()
    if settings.env == "production" or not settings.allow_development_actor_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC authentication is not connected yet",
        )
    try:
        principal_id = uuid.UUID(str(x_chat_principal_id or ""))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="valid X-Chat-Principal-Id header required",
        ) from error
    return Actor(principal_id=principal_id)


async def require_admin(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
    settings = get_settings()
    if not settings.literature_service_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="system-role verification is not configured",
        )
    try:
        async with httpx.AsyncClient(
            base_url=settings.literature_api_base_url.rstrip("/"),
            timeout=settings.literature_api_timeout_seconds,
            headers={
                "X-Literature-Service-Token": settings.literature_service_token,
                "X-Act-As-Principal-Id": str(actor.principal_id),
            },
        ) as client:
            response = await client.get("/auth/session")
        response.raise_for_status()
        system_role = str(response.json().get("principal", {}).get("system_role") or "USER")
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {401, 403}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="admin role required"
            ) from error
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="system-role verification failed",
        ) from error
    except (httpx.HTTPError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="system-role verification failed",
        ) from error
    if system_role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return actor
