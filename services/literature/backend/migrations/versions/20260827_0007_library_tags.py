"""Add tenant-scoped tags for M2 catalogue organization.

Revision ID: 20260827_0007
Revises: 20260826_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0007"
down_revision: str | None = "20260826_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_tags",
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('ACTIVE','DELETED')"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tag_id"),
        sa.UniqueConstraint("library_id", "tag_id", name="uq_library_tag_scope"),
    )
    op.create_index(
        "uq_active_library_tag_name",
        "library_tags",
        ["library_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "item_tags",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["added_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_item_tag_item_scope",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "tag_id"],
            ["library_tags.library_id", "library_tags.tag_id"],
            ondelete="CASCADE",
            name="fk_item_tag_tag_scope",
        ),
        sa.PrimaryKeyConstraint("library_id", "tag_id", "library_item_id"),
    )
    op.execute(
        """
        ALTER TABLE library_tags ENABLE ROW LEVEL SECURITY;
        ALTER TABLE item_tags ENABLE ROW LEVEL SECURITY;

        CREATE POLICY library_tags_select ON library_tags FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY library_tags_insert ON library_tags FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY library_tags_update ON library_tags FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));

        CREATE POLICY item_tags_select ON item_tags FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY item_tags_insert ON item_tags FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY item_tags_delete ON item_tags FOR DELETE
            USING (app_security.can_edit_library(library_id));
        """
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT, INSERT, UPDATE ON library_tags TO literature_app;
                GRANT SELECT, INSERT, DELETE ON item_tags TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_table("item_tags")
    op.drop_index("uq_active_library_tag_name", table_name="library_tags")
    op.drop_table("library_tags")
