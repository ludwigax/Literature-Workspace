from __future__ import annotations

from ..config import Settings
from .base import FunctionTool, ToolContext, ToolRegistry, ToolResult
from .literature import (
    DocumentGetByDoiTool,
    DocumentRetrievalTool,
    LiteratureClient,
)
from .plan_board import PlanBoardTool


def build_tool_registry(settings: Settings) -> ToolRegistry:
    literature = LiteratureClient(
        base_url=settings.literature_api_base_url,
        timeout=settings.literature_api_timeout_seconds,
        service_token=settings.literature_service_token,
    )
    return ToolRegistry(
        [
            PlanBoardTool(),
            DocumentRetrievalTool(literature),
            DocumentGetByDoiTool(literature),
        ]
    )


__all__ = [
    "FunctionTool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_tool_registry",
]
