"""Create identity and Library tenant boundary.

Revision ID: 20260824_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('ACTIVE','SUSPENDED','DELETED')"),
        sa.PrimaryKeyConstraint("principal_id"),
    )
    op.create_table(
        "external_identities",
        sa.Column("external_identity_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.principal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("external_identity_id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_external_identity"),
    )
    op.create_table(
        "libraries",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("library_type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("library_type IN ('PERSONAL','GROUP')"),
        sa.CheckConstraint("status IN ('ACTIVE','ARCHIVED','DELETED')"),
        sa.ForeignKeyConstraint(
            ["owner_principal_id"], ["principals.principal_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("library_id"),
    )
    op.create_table(
        "library_memberships",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('OWNER','EDITOR','VIEWER')"),
        sa.CheckConstraint("status IN ('ACTIVE','INVITED','REVOKED')"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.principal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("library_id", "principal_id"),
    )
    op.create_index(
        "ix_memberships_principal_status",
        "library_memberships",
        ["principal_id", "status"],
    )
    op.create_table(
        "web_sessions",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oidc_refresh_token_ciphertext", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.principal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_web_sessions_principal_expires",
        "web_sessions",
        ["principal_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_web_sessions_principal_expires", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_index("ix_memberships_principal_status", table_name="library_memberships")
    op.drop_table("library_memberships")
    op.drop_table("libraries")
    op.drop_table("external_identities")
    op.drop_table("principals")
