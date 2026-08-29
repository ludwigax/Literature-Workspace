"""Add OIDC login attempts and CSRF-bound sessions.

Revision ID: 20260825_0002
Revises: 20260824_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0002"
down_revision: str | None = "20260824_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_personal_library_owner",
        "libraries",
        ["owner_principal_id"],
        unique=True,
        postgresql_where=sa.text("library_type = 'PERSONAL' AND status <> 'DELETED'"),
    )
    op.add_column(
        "web_sessions",
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=True),
    )
    op.execute("UPDATE web_sessions SET csrf_token_hash = repeat('0', 64), revoked_at = now()")
    op.alter_column("web_sessions", "csrf_token_hash", nullable=False)

    op.create_table(
        "oidc_login_attempts",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.String(), nullable=False),
        sa.Column("return_path", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_oidc_attempts_expires", "oidc_login_attempts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_oidc_attempts_expires", table_name="oidc_login_attempts")
    op.drop_table("oidc_login_attempts")
    op.drop_column("web_sessions", "csrf_token_hash")
    op.drop_index("uq_personal_library_owner", table_name="libraries")
