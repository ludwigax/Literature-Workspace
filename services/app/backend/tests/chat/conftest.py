from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException, Request, status
from sqlalchemy import delete

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from backend.app.authorization.dependencies import Actor, current_actor, require_csrf
from backend.app.chat.models import ChatSession
from backend.app.database import session_factory
from backend.app.main import app
from backend.app.models import Principal


async def _test_actor(request: Request) -> Actor:
    raw_principal_id = request.headers.get("X-Chat-Principal-Id")
    if raw_principal_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    principal_id = uuid.UUID(raw_principal_id)
    async with session_factory() as session, session.begin():
        if await session.get(Principal, principal_id) is None:
            session.add(
                Principal(
                    principal_id=principal_id,
                    display_name=f"Chat test {principal_id}",
                    status="ACTIVE",
                )
            )
    return Actor(
        principal_id=principal_id,
        display_name=f"Chat test {principal_id}",
        session_id=uuid.UUID(int=0),
    )


async def _skip_csrf() -> None:
    return None


@pytest.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    app.dependency_overrides[current_actor] = _test_actor
    app.dependency_overrides[require_csrf] = _skip_csrf
    async with session_factory() as session, session.begin():
        await session.execute(delete(ChatSession))
    yield
    app.dependency_overrides.pop(current_actor, None)
    app.dependency_overrides.pop(require_csrf, None)
