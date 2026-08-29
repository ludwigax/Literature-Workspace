from __future__ import annotations

import asyncio
import sys

import pytest
from sqlalchemy import delete

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from backend.app.database import api_session_factory
from backend.app.models import ChatSession


@pytest.fixture(autouse=True)
async def clean_database() -> None:
    async with api_session_factory() as session, session.begin():
        await session.execute(delete(ChatSession))
