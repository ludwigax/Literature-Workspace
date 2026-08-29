"""persist administrator-controlled tool runtime configuration

Revision ID: 20260917_0028
Revises: 20260916_0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260917_0028"
down_revision: str | None = "20260916_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_runtime_configs",
        sa.Column("tool_name", sa.String(300), primary_key=True),
        sa.Column("config_json", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principals.principal_id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON tool_runtime_configs "
        "TO literature_app, chat_worker"
    )


def downgrade() -> None:
    op.drop_table("tool_runtime_configs")
