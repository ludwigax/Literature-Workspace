"""Create append-only audit events.

Revision ID: 20260825_0004
Revises: 20260825_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260825_0004"
down_revision: str | None = "20260825_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=True),
        sa.Column("subject_principal_id", sa.Uuid(), nullable=True),
        sa.Column("library_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"], ["principals.principal_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["subject_principal_id"], ["principals.principal_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["web_sessions.session_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_audit_events_principal_time",
        "audit_events",
        ["actor_principal_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_library_time",
        "audit_events",
        ["library_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_type_time",
        "audit_events",
        ["event_type", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_type_time", table_name="audit_events")
    op.drop_index("ix_audit_events_library_time", table_name="audit_events")
    op.drop_index("ix_audit_events_principal_time", table_name="audit_events")
    op.drop_table("audit_events")
