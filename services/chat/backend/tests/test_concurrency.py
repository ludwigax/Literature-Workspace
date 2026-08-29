from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from backend.app.config import get_settings
from backend.app.database import worker_session_factory
from backend.app.execution import TurnExecutor
from backend.app.main import app
from backend.app.providers import FakeResponsesProvider


async def _queue_turn(client: AsyncClient, actor_id: uuid.UUID, title: str) -> str:
    headers = {"X-Chat-Principal-Id": str(actor_id)}
    created = await client.post(
        "/api/chat/v1/sessions", json={"title": title}, headers=headers
    )
    chat_session = created.json()["session"]
    queued = await client.post(
        f"/api/chat/v1/sessions/{chat_session['session_id']}/turns",
        json={"content": title, "base_revision": 0},
        headers=headers,
    )
    assert queued.status_code == 202, queued.text
    return str(queued.json()["turn"]["turn_id"])


async def test_claiming_enforces_three_running_turns_per_principal_without_global_blocking(
) -> None:
    first_actor = uuid.uuid4()
    second_actor = uuid.uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_actor_turns = [
            await _queue_turn(client, first_actor, f"first-{index}")
            for index in range(4)
        ]
        second_actor_turn = await _queue_turn(client, second_actor, "second")

    executor = TurnExecutor(
        session_factory=worker_session_factory,
        provider=FakeResponsesProvider(),
        settings=get_settings(),
        worker_id="capacity-test-worker",
    )
    claimed = [str(await executor.claim_next_turn()) for _ in range(4)]
    assert claimed[:3] == first_actor_turns[:3]
    assert claimed[3] == second_actor_turn
    assert first_actor_turns[3] not in claimed
