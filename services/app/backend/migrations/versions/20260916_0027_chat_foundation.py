"""chat foundation

Revision ID: 20260916_0027
Revises: 20260915_0026
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260916_0027"
down_revision: str | None = "20260915_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principals.principal_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED','DELETED')",
            name="chat_sessions_status_check",
        ),
    )
    op.create_index(
        "ix_chat_sessions_owner_updated",
        "chat_sessions",
        ["owner_principal_id", "updated_at"],
    )
    op.create_table(
        "conversation_units",
        sa.Column("unit_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_units.unit_id", ondelete="RESTRICT"),
        ),
        sa.Column("unit_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("model_step_id", postgresql.UUID(as_uuid=True)),
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=False),
        sa.Column("interrupted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "unit_type IN ('USER_INPUT','MODEL_RESPONSE')",
            name="conversation_units_unit_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','SETTLED')", name="conversation_units_status_check"
        ),
    )
    op.create_index(
        "ix_conversation_units_session_created",
        "conversation_units",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_conversation_units_parent", "conversation_units", ["parent_unit_id"]
    )
    op.create_table(
        "chat_branches",
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "root_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_units.unit_id", ondelete="SET NULL"),
        ),
        sa.Column(
            "head_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_units.unit_id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_from_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_units.unit_id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "name", name="uq_chat_branch_name"),
    )
    op.create_index(
        "ix_chat_branches_session", "chat_branches", ["session_id", "created_at"]
    )
    op.create_table(
        "turn_runs",
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_branches.branch_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principals.principal_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "input_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_units.unit_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "final_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_units.unit_id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("used_tool_calls", sa.Integer(), nullable=False),
        sa.Column("completion_reason", sa.String(80), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_tool_calls >= 0", name="turn_runs_max_tool_calls_check"
        ),
        sa.CheckConstraint(
            "used_tool_calls >= 0", name="turn_runs_used_tool_calls_check"
        ),
        sa.CheckConstraint(
            "status IN ('WAITING','STARTING','RUNNING_MODEL','RUNNING_TOOLS',"
            "'INTERRUPT_REQUESTED','COMPLETED','INTERRUPTED_PARTIAL','FAILED')",
            name="turn_runs_status_check",
        ),
    )
    op.create_index("ix_turn_runs_queue", "turn_runs", ["status", "created_at"])
    op.create_index(
        "ix_turn_runs_owner_status", "turn_runs", ["owner_principal_id", "status"]
    )
    op.create_index(
        "uq_turn_runs_one_nonterminal_per_session",
        "turn_runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('WAITING','STARTING','RUNNING_MODEL','RUNNING_TOOLS',"
            "'INTERRUPT_REQUESTED')"
        ),
    )
    op.create_table(
        "model_steps",
        sa.Column("step_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("turn_runs.turn_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("provider_response_id", sa.String(300), nullable=False),
        sa.Column("input_items_json", postgresql.JSONB(), nullable=False),
        sa.Column("raw_response_json", postgresql.JSONB(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETED','INTERRUPTED','FAILED')",
            name="model_steps_status_check",
        ),
        sa.UniqueConstraint("turn_id", "ordinal", name="uq_model_steps_turn_ordinal"),
    )
    op.create_table(
        "model_output_items",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_steps.step_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("provider_item_id", sa.String(300), nullable=False),
        sa.Column("item_type", sa.String(80), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("step_id", "ordinal", name="uq_model_output_items_step_ordinal"),
    )
    op.create_index(
        "ix_model_output_items_provider_id", "model_output_items", ["provider_item_id"]
    )
    op.create_table(
        "tool_executions",
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("turn_runs.turn_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_steps.step_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_output_items.item_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_id", sa.String(300), nullable=False),
        sa.Column("tool_name", sa.String(300), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("error_json", postgresql.JSONB(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(300), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')",
            name="tool_executions_status_check",
        ),
        sa.UniqueConstraint("turn_id", "call_id", name="uq_tool_execution_turn_call"),
    )
    op.create_table(
        "turn_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("turn_runs.turn_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("turn_id", "sequence_no", name="uq_turn_events_turn_sequence"),
    )
    op.create_index(
        "ix_turn_events_turn_sequence", "turn_events", ["turn_id", "sequence_no"]
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON chat_sessions, conversation_units, "
        "chat_branches, turn_runs, model_steps, model_output_items, tool_executions, "
        "turn_events TO literature_app, chat_worker"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE turn_events_event_id_seq "
        "TO literature_app, chat_worker"
    )
    op.execute(
        "GRANT SELECT ON principals, canonical_papers, canonical_identifiers, "
        "canonical_metadata, blobs, document_databases, document_database_releases, "
        "document_release_indexes, document_index_manifest_rows, "
        "document_index_facet_bitmaps, pipeline_documents, document_chunks "
        "TO chat_worker"
    )


def downgrade() -> None:
    op.drop_table("turn_events")
    op.drop_table("tool_executions")
    op.drop_table("model_output_items")
    op.drop_table("model_steps")
    op.drop_table("turn_runs")
    op.drop_table("chat_branches")
    op.drop_table("conversation_units")
    op.drop_table("chat_sessions")
