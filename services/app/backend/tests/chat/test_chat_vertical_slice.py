from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.chat.execution import TurnExecutor
from backend.app.chat.models import ModelOutputItem, ModelStep
from backend.app.chat.providers import FakeResponsesProvider
from backend.app.config import get_settings
from backend.app.database import chat_worker_session_factory
from backend.app.main import app


async def test_fake_turn_persists_tree_steps_items_and_events() -> None:
    actor_id = uuid.uuid4()
    headers = {"X-Chat-Principal-Id": str(actor_id)}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/chat/v1/sessions", json={"title": "Test session"}, headers=headers
        )
        assert created.status_code == 201, created.text
        chat_session = created.json()["session"]
        session_id = chat_session["session_id"]

        queued = await client.post(
            f"/api/chat/v1/sessions/{session_id}/turns",
            json={
                "content": "Explain the evidence",
                "branch_id": chat_session["default_branch_id"],
                "base_revision": 0,
                "max_tool_calls": 0,
            },
            headers=headers,
        )
        assert queued.status_code == 202, queued.text
        turn_id = queued.json()["turn"]["turn_id"]

        conflict = await client.post(
            f"/api/chat/v1/sessions/{session_id}/turns",
            json={"content": "A second active turn", "base_revision": 1},
            headers=headers,
        )
        assert conflict.status_code == 409

        settings = get_settings()
        executor = TurnExecutor(
            session_factory=chat_worker_session_factory,
            provider=FakeResponsesProvider(prefix="Accepted", delay_seconds=0),
            settings=settings,
            worker_id="test-worker",
        )
        claimed = await executor.claim_next_turn()
        assert str(claimed) == turn_id
        await executor.execute(uuid.UUID(turn_id))

        turn_response = await client.get(
            f"/api/chat/v1/turns/{turn_id}", headers=headers
        )
        assert turn_response.status_code == 200
        assert turn_response.json()["turn"]["status"] == "COMPLETED"

        graph_response = await client.get(
            f"/api/chat/v1/sessions/{session_id}/graph", headers=headers
        )
        assert graph_response.status_code == 200
        graph = graph_response.json()
        assert [unit["unit_type"] for unit in graph["units"]] == [
            "USER_INPUT",
            "MODEL_RESPONSE",
        ]
        assert graph["units"][1]["display_text"] == "Accepted: Explain the evidence"
        assert graph["branches"][0]["head_unit_id"] == graph["units"][1]["unit_id"]

        events_response = await client.get(
            f"/api/chat/v1/turns/{turn_id}/events?after=0", headers=headers
        )
        event_types = [event["type"] for event in events_response.json()["events"]]
        assert event_types[0] == "turn.queued"
        assert "response.output_text.delta" in event_types
        assert event_types[-1] == "turn.completed"

        other_actor = {"X-Chat-Principal-Id": str(uuid.uuid4())}
        hidden = await client.get(f"/api/chat/v1/turns/{turn_id}", headers=other_actor)
        assert hidden.status_code == 404

        follow_up = await client.post(
            f"/api/chat/v1/sessions/{session_id}/turns",
            json={"content": "Continue from that answer", "base_revision": 2},
            headers=headers,
        )
        assert follow_up.status_code == 202, follow_up.text
        follow_up_turn_id = follow_up.json()["turn"]["turn_id"]
        assert str(await executor.claim_next_turn()) == follow_up_turn_id
        await executor.execute(uuid.UUID(follow_up_turn_id))

    async with chat_worker_session_factory() as session:
        step_count = await session.scalar(
            select(func.count(ModelStep.step_id)).where(
                ModelStep.turn_id == uuid.UUID(turn_id)
            )
        )
        item = await session.scalar(
            select(ModelOutputItem)
            .join(ModelStep, ModelStep.step_id == ModelOutputItem.step_id)
            .where(ModelStep.turn_id == uuid.UUID(turn_id))
        )
        assert step_count == 1
        assert item is not None
        assert item.item_type == "message"
        assert item.payload_json["content"][0]["text"] == "Accepted: Explain the evidence"
        follow_up_step = await session.scalar(
            select(ModelStep).where(ModelStep.turn_id == uuid.UUID(follow_up_turn_id))
        )
        assert follow_up_step is not None
        assert [item.get("role", item.get("type")) for item in follow_up_step.input_items_json] == [
            "user",
            "assistant",
            "user",
        ]
