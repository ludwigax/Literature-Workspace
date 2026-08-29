from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ConversationUnit, ModelOutputItem


class ContextMaterializer:
    async def build(
        self, session: AsyncSession, *, session_id: uuid.UUID, leaf_unit_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        chain = await self._chain(session, session_id=session_id, leaf_unit_id=leaf_unit_id)
        items: list[dict[str, Any]] = []
        for unit in chain:
            if unit.unit_type == "USER_INPUT":
                items.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": unit.display_text}],
                    }
                )
                continue
            if unit.model_step_id is None:
                raise RuntimeError(f"MODEL_RESPONSE unit {unit.unit_id} has no model_step_id")
            output_items = (
                await session.scalars(
                    select(ModelOutputItem)
                    .where(ModelOutputItem.step_id == unit.model_step_id)
                    .order_by(ModelOutputItem.ordinal)
                )
            ).all()
            items.extend(dict(item.payload_json) for item in output_items)
        return items

    @staticmethod
    async def _chain(
        session: AsyncSession, *, session_id: uuid.UUID, leaf_unit_id: uuid.UUID
    ) -> list[ConversationUnit]:
        result: list[ConversationUnit] = []
        current_id: uuid.UUID | None = leaf_unit_id
        seen: set[uuid.UUID] = set()
        while current_id is not None:
            if current_id in seen:
                raise RuntimeError("conversation unit cycle detected")
            seen.add(current_id)
            unit = await session.get(ConversationUnit, current_id)
            if unit is None or unit.session_id != session_id:
                raise RuntimeError("conversation path contains an unavailable unit")
            if unit.status != "SETTLED":
                raise RuntimeError("conversation context contains an unsettled unit")
            result.append(unit)
            current_id = unit.parent_unit_id
        result.reverse()
        return result
