from __future__ import annotations

from typing import Any

from backend.app.chat.providers import ModelRequest, OpenAIResponsesProvider
from backend.app.config import Settings


class _Event:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.type = payload["type"]

    def model_dump(self, **_: object) -> dict[str, Any]:
        return dict(self.payload)


class _Stream:
    def __init__(self, events: list[_Event]) -> None:
        self.events = events

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> _Event:
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


class _Responses:
    def __init__(self) -> None:
        self.arguments: dict[str, Any] = {}

    async def create(self, **arguments: Any) -> _Stream:
        self.arguments = arguments
        return _Stream(
            [
                _Event(
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": 3,
                        "delta": "hello",
                    }
                )
            ]
        )


class _CompletedWithoutOutputResponses(_Responses):
    async def create(self, **arguments: Any) -> _Stream:
        self.arguments = arguments
        call = {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "plan_board",
            "arguments": "{}",
        }
        return _Stream(
            [
                _Event(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": call,
                    }
                ),
                _Event(
                    {
                        "type": "response.completed",
                        "response": {"id": "resp_1", "output": []},
                    }
                ),
            ]
        )


class _Client:
    def __init__(self) -> None:
        self.responses = _Responses()


async def test_openai_provider_preserves_event_payload_and_uses_full_history() -> None:
    client = _Client()
    provider = OpenAIResponsesProvider(Settings(), client=client)  # type: ignore[arg-type]
    events = [
        event
        async for event in provider.stream(
            ModelRequest(
                model="test-model",
                input_items=[{"role": "user", "content": "hello"}],
                tools=[],
                allow_tools=False,
            )
        )
    ]

    assert client.responses.arguments == {
        "model": "test-model",
        "input": [{"role": "user", "content": "hello"}],
        "stream": True,
        "store": False,
    }
    assert events[0].event_type == "response.output_text.delta"
    assert events[0].payload == {"sequence_number": 3, "delta": "hello"}


async def test_openai_provider_rebuilds_missing_completed_output_from_done_items() -> None:
    client = _Client()
    client.responses = _CompletedWithoutOutputResponses()
    provider = OpenAIResponsesProvider(Settings(), client=client)  # type: ignore[arg-type]

    events = [
        event
        async for event in provider.stream(
            ModelRequest(
                model="test-model",
                input_items=[{"role": "user", "content": "hello"}],
                tools=[],
                allow_tools=False,
            )
        )
    ]

    assert events[-1].payload["response"]["output"] == [
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "plan_board",
            "arguments": "{}",
        }
    ]
