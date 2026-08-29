from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...config import Settings
from .base import FunctionTool, ToolContext, ToolRegistry, ToolResult
from .literature import DocumentGetByDoiTool, DocumentRetrievalTool
from .plan_board import PlanBoardTool


def build_tool_registry(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> ToolRegistry:
    del settings
    return ToolRegistry(
        [
            PlanBoardTool(),
            DocumentRetrievalTool(session_factory),
            DocumentGetByDoiTool(session_factory),
        ]
    )


__all__ = [
    "FunctionTool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_tool_registry",
]
