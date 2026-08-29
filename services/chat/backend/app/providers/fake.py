from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .base import ModelRequest, ProviderEvent


class FakeResponsesProvider:
    name = "fake"

    def __init__(self, *, prefix: str = "Fake response", delay_seconds: float = 0.0) -> None:
        self.prefix = prefix
        self.delay_seconds = delay_seconds

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        response_id = f"resp_fake_{uuid.uuid4().hex}"
        item_id = f"msg_fake_{uuid.uuid4().hex}"
        text = f"{self.prefix}: {self._last_user_text(request.input_items)}"
        yield ProviderEvent("response.created", {"response_id": response_id})
        for chunk in self._chunks(text):
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield ProviderEvent(
                "response.output_text.delta",
                {"response_id": response_id, "item_id": item_id, "delta": chunk},
            )
        output_item: dict[str, Any] = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": text, "annotations": []}
            ],
        }
        yield ProviderEvent(
            "response.completed",
            {
                "response": {
                    "id": response_id,
                    "model": request.model,
                    "output": [output_item],
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
            },
        )

    @staticmethod
    def _last_user_text(items: list[dict[str, Any]]) -> str:
        for item in reversed(items):
            if item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "input_text"
                ).strip()
        return ""

    @staticmethod
    def _chunks(text: str, size: int = 12) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)]
