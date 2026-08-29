from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ToolSource = Literal["FUNCTION", "MCP"]


@dataclass(frozen=True)
class ToolContext:
    turn_id: uuid.UUID
    principal_id: uuid.UUID
    runtime_config: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    data: dict[str, Any]

    def model_output(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))


class FunctionTool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    source_type: ToolSource

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, tools: list[FunctionTool]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": True,
            }
            for tool in self._tools.values()
        ]

    def require(self, name: str) -> FunctionTool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise LookupError(f"unknown tool: {name}") from error
