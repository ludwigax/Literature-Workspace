"""Create the M2 catalogue and Collection core.

Revision ID: 20260826_0006
Revises: 20260825_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0006"
down_revision: str | None = "20260825_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "canonical_papers",
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_metadata_version_id", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('ACTIVE','MERGED','DELETED')"),
        sa.PrimaryKeyConstraint("canonical_paper_id"),
    )
    op.create_table(
        "canonical_metadata_versions",
        sa.Column("metadata_version_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("publication_year", sa.SmallInteger(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("venue", sa.Text(), nullable=True),
        sa.Column(
            "authors", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "extra", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("publication_year IS NULL OR publication_year BETWEEN 1000 AND 3000"),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("metadata_version_id"),
        sa.UniqueConstraint("canonical_paper_id", "version", name="uq_canonical_metadata_version"),
    )
    op.create_foreign_key(
        "fk_canonical_current_metadata",
        "canonical_papers",
        "canonical_metadata_versions",
        ["current_metadata_version_id"],
        ["metadata_version_id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "canonical_identifiers",
        sa.Column("identifier_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("normalized_value", sa.String(length=500), nullable=False),
        sa.Column("original_value", sa.String(length=500), nullable=False),
        *timestamps(),
        sa.CheckConstraint("scheme IN ('DOI','PMID','ARXIV','ISBN','OTHER')"),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("identifier_id"),
        sa.UniqueConstraint("scheme", "normalized_value", name="uq_canonical_identifier"),
    )
    op.create_table(
        "library_items",
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "local_overrides",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("saved_by", sa.Uuid(), nullable=True),
        sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trashed_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("item_type IN ('PAPER')"),
        sa.CheckConstraint("status IN ('ACTIVE','TRASHED','PURGED')"),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["saved_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trashed_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("library_item_id"),
        sa.UniqueConstraint("library_id", "library_item_id", name="uq_library_item_scope"),
    )
    op.create_index(
        "uq_active_library_canonical_paper",
        "library_items",
        ["library_id", "canonical_paper_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'PURGED'"),
    )
    op.create_index(
        "ix_library_items_library_status_created",
        "library_items",
        ["library_id", "status", "created_at"],
    )
    op.create_table(
        "collections",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("parent_collection_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('ACTIVE','DELETED')"),
        sa.CheckConstraint("parent_collection_id IS NULL OR parent_collection_id <> collection_id"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collection_id"),
        sa.UniqueConstraint("library_id", "collection_id", name="uq_collection_scope"),
    )
    op.create_foreign_key(
        "fk_collection_parent_scope",
        "collections",
        "collections",
        ["library_id", "parent_collection_id"],
        ["library_id", "collection_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_collections_library_parent",
        "collections",
        ["library_id", "parent_collection_id", "name"],
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_active_collection_sibling_name
        ON collections (
            library_id,
            coalesce(parent_collection_id, '00000000-0000-0000-0000-000000000000'::uuid),
            lower(name)
        )
        WHERE status = 'ACTIVE'
        """
    )
    op.create_table(
        "collection_items",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["added_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["library_id", "collection_id"],
            ["collections.library_id", "collections.collection_id"],
            ondelete="CASCADE",
            name="fk_collection_item_collection_scope",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_collection_item_item_scope",
        ),
        sa.PrimaryKeyConstraint("library_id", "collection_id", "library_item_id"),
    )

    op.execute(
        """
        ALTER TABLE library_items ENABLE ROW LEVEL SECURITY;
        ALTER TABLE collections ENABLE ROW LEVEL SECURITY;
        ALTER TABLE collection_items ENABLE ROW LEVEL SECURITY;

        CREATE POLICY library_items_select ON library_items FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY library_items_insert ON library_items FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY library_items_update ON library_items FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));

        CREATE POLICY collections_select ON collections FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY collections_insert ON collections FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY collections_update ON collections FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));

        CREATE POLICY collection_items_select ON collection_items FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY collection_items_insert ON collection_items FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY collection_items_delete ON collection_items FOR DELETE
            USING (app_security.can_edit_library(library_id));
        """
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT, INSERT, UPDATE ON canonical_papers TO literature_app;
                GRANT SELECT, INSERT ON canonical_identifiers TO literature_app;
                GRANT SELECT, INSERT ON canonical_metadata_versions TO literature_app;
                GRANT SELECT, INSERT, UPDATE ON library_items, collections TO literature_app;
                GRANT SELECT, INSERT, DELETE ON collection_items TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_table("collection_items")
    op.drop_index("uq_active_collection_sibling_name", table_name="collections")
    op.drop_index("ix_collections_library_parent", table_name="collections")
    op.drop_table("collections")
    op.drop_index("ix_library_items_library_status_created", table_name="library_items")
    op.drop_index("uq_active_library_canonical_paper", table_name="library_items")
    op.drop_table("library_items")
    op.drop_table("canonical_identifiers")
    op.drop_constraint("fk_canonical_current_metadata", "canonical_papers", type_="foreignkey")
    op.drop_table("canonical_metadata_versions")
    op.drop_table("canonical_papers")
