from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolResult, ToolSource


class PlanBoardTool:
    name = "plan_board"
    description = (
        "Create or replace the plan for the current turn. Keep exactly one step "
        "in_progress while work remains, and mark every step completed when done."
    )
    source_type: ToolSource = "FUNCTION"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "explanation": {"type": ["string", "null"]},
            "plan": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["explanation", "plan"],
        "additionalProperties": False,
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        raw_plan = arguments.get("plan")
        if not isinstance(raw_plan, list) or not raw_plan:
            raise ValueError("plan must contain at least one step")
        plan: list[dict[str, str]] = []
        in_progress = 0
        for raw_step in raw_plan:
            if not isinstance(raw_step, dict):
                raise ValueError("each plan step must be an object")
            step = str(raw_step.get("step") or "").strip()
            status = str(raw_step.get("status") or "")
            if not step or status not in {"pending", "in_progress", "completed"}:
                raise ValueError("plan step or status is invalid")
            in_progress += int(status == "in_progress")
            plan.append({"step": step, "status": status})
        if in_progress > 1:
            raise ValueError("at most one plan step may be in_progress")
        explanation = arguments.get("explanation")
        return ToolResult(
            {
                "turn_id": str(context.turn_id),
                "explanation": str(explanation).strip() if explanation else None,
                "plan": plan,
            }
        )
