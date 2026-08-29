from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from ...config import Settings
from .base import ModelRequest, ProviderEvent, UpstreamStreamError


class OpenAIResponsesProvider:
    """Stream Responses events while leaving conversation state in our database."""

    name = "openai"

    def __init__(self, settings: Settings, *, client: AsyncOpenAI | None = None) -> None:
        if client is not None:
            self.client = client
            return
        options: dict[str, Any] = {"timeout": settings.chat_openai_timeout_seconds}
        if settings.chat_openai_api_key:
            options["api_key"] = settings.chat_openai_api_key.get_secret_value()
        if settings.chat_openai_base_url:
            options["base_url"] = settings.chat_openai_base_url
        if settings.chat_openai_organization:
            options["organization"] = settings.chat_openai_organization
        if settings.chat_openai_project:
            options["project"] = settings.chat_openai_project
        self.client = AsyncOpenAI(**options)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        arguments: dict[str, Any] = {
            "model": request.model,
            "input": request.input_items,
            "stream": True,
            "store": False,
        }
        if request.instructions:
            arguments["instructions"] = request.instructions
        if request.allow_tools and request.tools:
            arguments["tools"] = request.tools
        completed_items: list[dict[str, Any]] = []
        try:
            stream = await self.client.responses.create(**arguments)
            async for event in stream:
                payload = event.model_dump(mode="json", exclude_none=True)
                event_type = str(payload.pop("type", getattr(event, "type", "response.event")))
                if event_type == "response.output_item.done":
                    item = payload.get("item")
                    if isinstance(item, dict):
                        completed_items.append(item)
                elif event_type == "response.completed":
                    response = payload.get("response")
                    if isinstance(response, dict) and not response.get("output"):
                        response["output"] = completed_items
                yield ProviderEvent(event_type=event_type, payload=payload)
        except Exception as error:
            raise UpstreamStreamError(
                f"OpenAI Responses stream failed: {type(error).__name__}: {error}"
            ) from error
