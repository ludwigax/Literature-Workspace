from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class ToolRuntimeConfig(TimestampMixin, Base):
    __tablename__ = "tool_runtime_configs"

    tool_name: Mapped[str] = mapped_column(String(300), primary_key=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ChatSession(TimestampMixin, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED','DELETED')",
            name="chat_sessions_status_check",
        ),
        Index("ix_chat_sessions_owner_updated", "owner_principal_id", "updated_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ConversationUnit(TimestampMixin, Base):
    __tablename__ = "conversation_units"
    __table_args__ = (
        CheckConstraint(
            "unit_type IN ('USER_INPUT','MODEL_RESPONSE')",
            name="conversation_units_unit_type_check",
        ),
        CheckConstraint(
            "status IN ('OPEN','SETTLED')", name="conversation_units_status_check"
        ),
        Index("ix_conversation_units_session_created", "session_id", "created_at"),
        Index("ix_conversation_units_parent", "parent_unit_id"),
    )

    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    parent_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_units.unit_id", ondelete="RESTRICT")
    )
    unit_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    display_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ChatBranch(TimestampMixin, Base):
    __tablename__ = "chat_branches"
    __table_args__ = (
        UniqueConstraint("session_id", "name", name="uq_chat_branch_name"),
        Index("ix_chat_branches_session", "session_id", "created_at"),
    )

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), default="main", nullable=False)
    root_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_units.unit_id", ondelete="SET NULL")
    )
    head_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_units.unit_id", ondelete="SET NULL")
    )
    created_from_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_units.unit_id", ondelete="SET NULL")
    )


class TurnRun(TimestampMixin, Base):
    __tablename__ = "turn_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('WAITING','STARTING','RUNNING_MODEL','RUNNING_TOOLS',"
            "'INTERRUPT_REQUESTED','COMPLETED','INTERRUPTED_PARTIAL','FAILED')",
            name="turn_runs_status_check",
        ),
        CheckConstraint("max_tool_calls >= 0", name="turn_runs_max_tool_calls_check"),
        CheckConstraint("used_tool_calls >= 0", name="turn_runs_used_tool_calls_check"),
        Index("ix_turn_runs_queue", "status", "created_at"),
        Index("ix_turn_runs_owner_status", "owner_principal_id", "status"),
        Index(
            "uq_turn_runs_one_nonterminal_per_session",
            "session_id",
            unique=True,
            postgresql_where=text(
                "status IN ('WAITING','STARTING','RUNNING_MODEL','RUNNING_TOOLS',"
                "'INTERRUPT_REQUESTED')"
            ),
        ),
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_branches.branch_id", ondelete="CASCADE"), nullable=False
    )
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    input_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_units.unit_id", ondelete="RESTRICT"), nullable=False
    )
    final_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_units.unit_id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_reason: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelStep(TimestampMixin, Base):
    __tablename__ = "model_steps"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING','COMPLETED','INTERRUPTED','FAILED')",
            name="model_steps_status_check",
        ),
        UniqueConstraint("turn_id", "ordinal", name="uq_model_steps_turn_ordinal"),
    )

    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turn_runs.turn_id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_response_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    input_items_json: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    raw_response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class ModelOutputItem(TimestampMixin, Base):
    __tablename__ = "model_output_items"
    __table_args__ = (
        UniqueConstraint("step_id", "ordinal", name="uq_model_output_items_step_ordinal"),
        Index("ix_model_output_items_provider_id", "provider_item_id"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_steps.step_id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_item_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    item_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ToolExecution(TimestampMixin, Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')",
            name="tool_executions_status_check",
        ),
        UniqueConstraint("turn_id", "call_id", name="uq_tool_execution_turn_call"),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turn_runs.turn_id", ondelete="CASCADE"), nullable=False
    )
    source_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_steps.step_id", ondelete="CASCADE"), nullable=False
    )
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_output_items.item_id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[str] = mapped_column(String(300), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(300), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TurnEvent(Base):
    __tablename__ = "turn_events"
    __table_args__ = (
        UniqueConstraint("turn_id", "sequence_no", name="uq_turn_events_turn_sequence"),
        Index("ix_turn_events_turn_sequence", "turn_id", "sequence_no"),
    )

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turn_runs.turn_id", ondelete="CASCADE"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
