from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from backend.app.chat.execution import TurnExecutor
from backend.app.chat.providers import FakeResponsesProvider
from backend.app.config import get_settings
from backend.app.database import chat_worker_session_factory
from backend.app.main import app


async def _finish_next(executor: TurnExecutor, expected_turn_id: str) -> None:
    claimed = await executor.claim_next_turn()
    assert str(claimed) == expected_turn_id
    await executor.execute(uuid.UUID(expected_turn_id))


async def test_sse_replays_from_our_cursor_and_closes_on_terminal_turn() -> None:
    actor_id = uuid.uuid4()
    headers = {"X-Chat-Principal-Id": str(actor_id)}
    transport = ASGITransport(app=app)
    executor = TurnExecutor(
        session_factory=chat_worker_session_factory,
        provider=FakeResponsesProvider(prefix="SSE", delay_seconds=0),
        settings=get_settings(),
        worker_id="sse-test-worker",
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/chat/v1/sessions", json={"title": "SSE"}, headers=headers
        )
        chat_session = created.json()["session"]
        queued = await client.post(
            f"/api/chat/v1/sessions/{chat_session['session_id']}/turns",
            json={"content": "stream me", "base_revision": 0},
            headers=headers,
        )
        turn_id = queued.json()["turn"]["turn_id"]
        await _finish_next(executor, turn_id)

        response = await client.get(
            f"/api/chat/v1/turns/{turn_id}/events/stream?after=2",
            headers={**headers, "Last-Event-ID": "3"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "id: 1\n" not in response.text
        assert "id: 3\n" not in response.text
        assert "event: response.completed\n" in response.text
        assert "event: turn.completed\n" in response.text


async def test_branch_from_old_unit_edit_and_regenerate_are_append_only() -> None:
    actor_id = uuid.uuid4()
    headers = {"X-Chat-Principal-Id": str(actor_id)}
    transport = ASGITransport(app=app)
    executor = TurnExecutor(
        session_factory=chat_worker_session_factory,
        provider=FakeResponsesProvider(prefix="Tree", delay_seconds=0),
        settings=get_settings(),
        worker_id="tree-test-worker",
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/chat/v1/sessions", json={"title": "Tree"}, headers=headers
        )
        chat_session = created.json()["session"]
        session_id = chat_session["session_id"]
        first = await client.post(
            f"/api/chat/v1/sessions/{session_id}/turns",
            json={"content": "original", "base_revision": 0},
            headers=headers,
        )
        await _finish_next(executor, first.json()["turn"]["turn_id"])
        graph = (await client.get(
            f"/api/chat/v1/sessions/{session_id}/graph", headers=headers
        )).json()
        original_user = graph["units"][0]
        original_answer = graph["units"][1]

        forked = await client.post(
            f"/api/chat/v1/sessions/{session_id}/turns",
            json={
                "content": "forked follow-up",
                "branch_id": chat_session["default_branch_id"],
                "parent_unit_id": original_user["unit_id"],
                "base_revision": 2,
            },
            headers=headers,
        )
        assert forked.status_code == 202, forked.text
        await _finish_next(executor, forked.json()["turn"]["turn_id"])

        edited = await client.post(
            f"/api/chat/v1/units/{original_user['unit_id']}/edit-and-regenerate",
            json={"content": "edited", "base_revision": 4},
            headers=headers,
        )
        assert edited.status_code == 202, edited.text
        await _finish_next(executor, edited.json()["turn"]["turn_id"])

        regenerated = await client.post(
            f"/api/chat/v1/units/{original_answer['unit_id']}/regenerate",
            json={"base_revision": 6},
            headers=headers,
        )
        assert regenerated.status_code == 202, regenerated.text
        await _finish_next(executor, regenerated.json()["turn"]["turn_id"])

        final_graph = (await client.get(
            f"/api/chat/v1/sessions/{session_id}/graph", headers=headers
        )).json()
        assert len(final_graph["branches"]) == 4
        assert len(final_graph["units"]) == 7
        assert final_graph["units"][0]["display_text"] == "original"
        edited_unit = next(
            unit for unit in final_graph["units"] if unit["display_text"] == "edited"
        )
        assert edited_unit["parent_unit_id"] is None
        assert edited_unit["content"]["edited_from_unit_id"] == original_user["unit_id"]
