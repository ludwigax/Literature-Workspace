from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelRequest:
    model: str
    input_items: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    allow_tools: bool
    instructions: str | None = None


@dataclass(frozen=True)
class ProviderEvent:
    event_type: str
    payload: dict[str, Any]


class ResponsesProvider(Protocol):
    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]: ...


class UpstreamStreamError(RuntimeError):
    """The provider stream cannot be resumed by the chat worker."""
