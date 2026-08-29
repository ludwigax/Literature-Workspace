from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .domain import TERMINAL_TURN_STATUSES
from .models import TurnEvent, TurnRun
from .service import ChatService


def encode_sse(event: dict[str, object]) -> str:
    payload = json.dumps(event["payload"], ensure_ascii=False, separators=(",", ":"))
    return f'id: {event["sequence"]}\nevent: {event["type"]}\ndata: {payload}\n\n'


async def stream_turn_events(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    owner_principal_id: uuid.UUID,
    turn_id: uuid.UUID,
    after: int,
    poll_seconds: float,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    cursor = after
    last_write = time.monotonic()
    yield "retry: 2000\n\n"
    while True:
        async with session_factory() as session:
            turn = await session.scalar(
                select(TurnRun).where(
                    TurnRun.turn_id == turn_id,
                    TurnRun.owner_principal_id == owner_principal_id,
                )
            )
            if turn is None:
                return
            rows = (
                await session.scalars(
                    select(TurnEvent)
                    .where(TurnEvent.turn_id == turn_id, TurnEvent.sequence_no > cursor)
                    .order_by(TurnEvent.sequence_no)
                )
            ).all()
            terminal = turn.status in {status.value for status in TERMINAL_TURN_STATUSES}

        for row in rows:
            event = ChatService.event_dict(row)
            cursor = row.sequence_no
            last_write = time.monotonic()
            yield encode_sse(event)

        if terminal:
            return
        if time.monotonic() - last_write >= heartbeat_seconds:
            last_write = time.monotonic()
            yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)
