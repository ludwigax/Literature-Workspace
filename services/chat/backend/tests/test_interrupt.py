from __future__ import annotations

import asyncio
import uuid

from httpx import ASGITransport, AsyncClient

from backend.app.config import get_settings
from backend.app.database import worker_session_factory
from backend.app.execution import TurnExecutor
from backend.app.main import app
from backend.app.providers import FakeResponsesProvider


async def test_interrupt_keeps_streamed_partial_reply_as_settled_tree_unit() -> None:
    actor_id = uuid.uuid4()
    headers = {"X-Chat-Principal-Id": str(actor_id)}
    transport = ASGITransport(app=app)
    executor = TurnExecutor(
        session_factory=worker_session_factory,
        provider=FakeResponsesProvider(
            prefix="A deliberately long partial response for interruption testing",
            delay_seconds=0.05,
        ),
        settings=get_settings(),
        worker_id="interrupt-test-worker",
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/chat/v1/sessions", json={"title": "Interrupt"}, headers=headers
        )
        chat_session = created.json()["session"]
        session_id = chat_session["session_id"]
        queued = await client.post(
            f"/api/chat/v1/sessions/{session_id}/turns",
            json={"content": "please continue for a while", "base_revision": 0},
            headers=headers,
        )
        turn_id = queued.json()["turn"]["turn_id"]
        assert str(await executor.claim_next_turn()) == turn_id
        execution = asyncio.create_task(executor.execute(uuid.UUID(turn_id)))

        for _ in range(100):
            events = await client.get(
                f"/api/chat/v1/turns/{turn_id}/events", headers=headers
            )
            if any(
                event["type"] == "response.output_text.delta"
                for event in events.json()["events"]
            ):
                break
            await asyncio.sleep(0.01)
        interrupted = await client.post(
            f"/api/chat/v1/turns/{turn_id}/interrupt", headers=headers
        )
        assert interrupted.status_code == 202, interrupted.text
        await execution

        turn = (await client.get(
            f"/api/chat/v1/turns/{turn_id}", headers=headers
        )).json()["turn"]
        assert turn["status"] == "INTERRUPTED_PARTIAL"
        graph = (await client.get(
            f"/api/chat/v1/sessions/{session_id}/graph", headers=headers
        )).json()
        partial = graph["units"][-1]
        assert partial["unit_type"] == "MODEL_RESPONSE"
        assert partial["interrupted"] is True
        assert partial["display_text"]
        assert turn["final_unit_id"] == partial["unit_id"]
