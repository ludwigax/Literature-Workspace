from __future__ import annotations

from enum import StrEnum


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"


class UnitType(StrEnum):
    USER_INPUT = "USER_INPUT"
    MODEL_RESPONSE = "MODEL_RESPONSE"


class UnitStatus(StrEnum):
    OPEN = "OPEN"
    SETTLED = "SETTLED"


class TurnStatus(StrEnum):
    WAITING = "WAITING"
    STARTING = "STARTING"
    RUNNING_MODEL = "RUNNING_MODEL"
    RUNNING_TOOLS = "RUNNING_TOOLS"
    INTERRUPT_REQUESTED = "INTERRUPT_REQUESTED"
    COMPLETED = "COMPLETED"
    INTERRUPTED_PARTIAL = "INTERRUPTED_PARTIAL"
    FAILED = "FAILED"


NONTERMINAL_TURN_STATUSES = frozenset(
    {
        TurnStatus.WAITING,
        TurnStatus.STARTING,
        TurnStatus.RUNNING_MODEL,
        TurnStatus.RUNNING_TOOLS,
        TurnStatus.INTERRUPT_REQUESTED,
    }
)
TERMINAL_TURN_STATUSES = frozenset(
    {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED_PARTIAL, TurnStatus.FAILED}
)


class StepStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


class ToolExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def can_transition_turn(current: TurnStatus, target: TurnStatus) -> bool:
    transitions = {
        TurnStatus.WAITING: {TurnStatus.STARTING, TurnStatus.INTERRUPT_REQUESTED},
        TurnStatus.STARTING: {
            TurnStatus.RUNNING_MODEL,
            TurnStatus.INTERRUPT_REQUESTED,
            TurnStatus.FAILED,
        },
        TurnStatus.RUNNING_MODEL: {
            TurnStatus.RUNNING_TOOLS,
            TurnStatus.COMPLETED,
            TurnStatus.INTERRUPT_REQUESTED,
            TurnStatus.FAILED,
        },
        TurnStatus.RUNNING_TOOLS: {
            TurnStatus.RUNNING_MODEL,
            TurnStatus.INTERRUPT_REQUESTED,
            TurnStatus.FAILED,
        },
        TurnStatus.INTERRUPT_REQUESTED: {TurnStatus.INTERRUPTED_PARTIAL},
        TurnStatus.COMPLETED: set(),
        TurnStatus.INTERRUPTED_PARTIAL: set(),
        TurnStatus.FAILED: set(),
    }
    return target in transitions[current]
