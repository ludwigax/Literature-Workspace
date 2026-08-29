from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import ToolRuntimeConfig

CONFIG_KEY = "literature_tools"


@dataclass(frozen=True)
class LiteratureToolConfig:
    retrieval_mode: Literal["BM25", "VECTOR", "HYBRID"]
    retrieval_top_k: int
    chunk_top_k_per_document: int
    doi_document_max_chars: int


def default_literature_tool_config(settings: Settings) -> LiteratureToolConfig:
    return LiteratureToolConfig(
        retrieval_mode=settings.literature_retrieval_mode,
        retrieval_top_k=settings.literature_retrieval_top_k,
        chunk_top_k_per_document=settings.literature_chunk_top_k_per_document,
        doi_document_max_chars=settings.literature_doi_document_max_chars,
    )


async def get_literature_tool_config(
    session: AsyncSession, settings: Settings
) -> tuple[LiteratureToolConfig, int]:
    stored = await session.get(ToolRuntimeConfig, CONFIG_KEY)
    defaults = asdict(default_literature_tool_config(settings))
    if stored is None:
        return LiteratureToolConfig(**defaults), 0
    merged = {**defaults, **stored.config_json}
    return LiteratureToolConfig(**merged), stored.revision


async def update_literature_tool_config(
    session: AsyncSession,
    settings: Settings,
    *,
    expected_revision: int,
    values: dict[str, Any],
    updated_by: uuid.UUID,
) -> tuple[LiteratureToolConfig, int]:
    stored = await session.get(ToolRuntimeConfig, CONFIG_KEY, with_for_update=True)
    current_revision = stored.revision if stored is not None else 0
    if current_revision != expected_revision:
        raise ValueError(f"tool config revision is {current_revision}, not {expected_revision}")
    defaults = asdict(default_literature_tool_config(settings))
    merged = {**defaults, **(stored.config_json if stored else {}), **values}
    config = LiteratureToolConfig(**merged)
    if stored is None:
        stored = ToolRuntimeConfig(
            tool_name=CONFIG_KEY,
            config_json=asdict(config),
            revision=1,
            updated_by=updated_by,
        )
        session.add(stored)
    else:
        stored.config_json = asdict(config)
        stored.revision += 1
        stored.updated_by = updated_by
    await session.commit()
    return config, stored.revision
