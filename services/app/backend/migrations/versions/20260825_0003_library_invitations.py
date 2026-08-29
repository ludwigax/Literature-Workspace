"""Create group Library invitations.

Revision ID: 20260825_0003
Revises: 20260825_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0003"
down_revision: str | None = "20260825_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_invitations",
        sa.Column("invitation_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('EDITOR','VIEWER')"),
        sa.CheckConstraint("status IN ('PENDING','ACCEPTED','REVOKED','EXPIRED')"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["principals.principal_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["accepted_by"], ["principals.principal_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("invitation_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_library_invitations_lookup",
        "library_invitations",
        ["library_id", "email_normalized", "status"],
    )
    op.create_index(
        "ix_library_invitations_expires",
        "library_invitations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_library_invitations_expires", table_name="library_invitations")
    op.drop_index("ix_library_invitations_lookup", table_name="library_invitations")
    op.drop_table("library_invitations")
