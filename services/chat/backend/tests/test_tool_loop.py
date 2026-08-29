from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.database import worker_session_factory
from backend.app.execution import TurnExecutor
from backend.app.main import app
from backend.app.models import ModelStep, ToolExecution
from backend.app.providers import ModelRequest, ProviderEvent


class ScriptedProvider:
    name = "scripted"

    def __init__(self, outputs: list[list[dict[str, Any]]]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        output = self.outputs[len(self.requests) - 1]
        response = {
            "id": f"resp_{len(self.requests)}",
            "model": request.model,
            "output": output,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        for item in output:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    yield ProviderEvent(
                        "response.output_text.delta",
                        {
                            "response_id": response["id"],
                            "item_id": item["id"],
                            "delta": content["text"],
                        },
                    )
        yield ProviderEvent("response.completed", {"response": response})


def function_call(call_id: str) -> dict[str, Any]:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": "plan_board",
        "arguments": json.dumps(
            {
                "explanation": "Start",
                "plan": [{"step": "Find evidence", "status": "in_progress"}],
            }
        ),
        "status": "completed",
    }


def message(text: str) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


async def _create_turn(client: AsyncClient, headers: dict[str, str], budget: int) -> str:
    created = await client.post(
        "/api/chat/v1/sessions", json={"title": "Tools"}, headers=headers
    )
    chat_session = created.json()["session"]
    queued = await client.post(
        f"/api/chat/v1/sessions/{chat_session['session_id']}/turns",
        json={"content": "Research this", "base_revision": 0, "max_tool_calls": budget},
        headers=headers,
    )
    assert queued.status_code == 202, queued.text
    return str(queued.json()["turn"]["turn_id"])


async def test_function_call_creates_tool_execution_and_second_model_step() -> None:
    headers = {"X-Chat-Principal-Id": str(uuid.uuid4())}
    provider = ScriptedProvider([[function_call("call_plan")], [message("Finished")]])
    executor = TurnExecutor(
        session_factory=worker_session_factory,
        provider=provider,
        settings=get_settings(),
        worker_id="tool-loop-worker",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn_id = await _create_turn(client, headers, 3)
        assert str(await executor.claim_next_turn()) == turn_id
        await executor.execute(uuid.UUID(turn_id))
        turn = (await client.get(
            f"/api/chat/v1/turns/{turn_id}", headers=headers
        )).json()["turn"]
        events = (await client.get(
            f"/api/chat/v1/turns/{turn_id}/events", headers=headers
        )).json()["events"]
        executions = (await client.get(
            f"/api/chat/v1/turns/{turn_id}/tool-executions", headers=headers
        )).json()["tool_executions"]

    assert turn["status"] == "COMPLETED"
    assert turn["used_tool_calls"] == 1
    assert len(provider.requests) == 2
    assert {schema["name"] for schema in provider.requests[0].tools} == {
        "plan_board",
        "document_retrieval",
        "document_get_by_doi",
    }
    assert provider.requests[1].input_items[-1]["type"] == "function_call_output"
    assert json.loads(provider.requests[1].input_items[-1]["output"])["ok"] is True
    assert "tool.execution.completed" in {event["type"] for event in events}
    assert executions[0]["result"]["output"]["plan"][0]["step"] == "Find evidence"
    async with worker_session_factory() as session:
        assert await session.scalar(
            select(func.count(ModelStep.step_id)).where(
                ModelStep.turn_id == uuid.UUID(turn_id)
            )
        ) == 2
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.turn_id == uuid.UUID(turn_id))
        )
        assert execution is not None
        assert execution.status == "COMPLETED"
        assert execution.tool_name == "plan_board"


async def test_tool_batch_over_remaining_budget_forces_no_tool_final_step() -> None:
    headers = {"X-Chat-Principal-Id": str(uuid.uuid4())}
    provider = ScriptedProvider(
        [
            [function_call("call_one"), function_call("call_two")],
            [message("Answered without more tools")],
        ]
    )
    executor = TurnExecutor(
        session_factory=worker_session_factory,
        provider=provider,
        settings=get_settings(),
        worker_id="tool-budget-worker",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn_id = await _create_turn(client, headers, 1)
        assert str(await executor.claim_next_turn()) == turn_id
        await executor.execute(uuid.UUID(turn_id))
        turn = (await client.get(
            f"/api/chat/v1/turns/{turn_id}", headers=headers
        )).json()["turn"]
        events = (await client.get(
            f"/api/chat/v1/turns/{turn_id}/events", headers=headers
        )).json()["events"]

    assert turn["status"] == "COMPLETED"
    assert turn["used_tool_calls"] == 0
    assert provider.requests[1].allow_tools is False
    assert provider.requests[1].instructions is not None
    assert "turn.tool_budget_exhausted" in {event["type"] for event in events}
