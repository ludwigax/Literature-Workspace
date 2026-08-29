"""Add Zotero snapshot import mappings and metadata source.

Revision ID: 20260905_0016
Revises: 20260904_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0016"
down_revision: str | None = "20260904_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "canonical_metadata_metadata_source_check",
        "canonical_metadata",
        type_="check",
    )
    op.create_check_constraint(
        "canonical_metadata_metadata_source_check",
        "canonical_metadata",
        "metadata_source IN ('UNDEFINED','CROSSREF','OPENALEX','ZOTERO')",
    )
    op.create_table(
        "zotero_import_sources",
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("source_identity", sa.String(300), nullable=False),
        sa.Column("display_name", sa.String(300)),
        sa.Column("last_imported_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("library_id", "source_identity", name="uq_zotero_source_identity"),
    )
    op.create_table(
        "zotero_import_entries",
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("zotero_library_id", sa.Integer(), primary_key=True),
        sa.Column("item_key", sa.String(32), primary_key=True),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("item_version", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(100), nullable=False),
        sa.Column("attachment_manifest", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["zotero_import_sources.source_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "zotero_collection_mappings",
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("zotero_library_id", sa.Integer(), primary_key=True),
        sa.Column("collection_key", sa.String(32), primary_key=True),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["zotero_import_sources.source_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["library_id", "collection_id"],
            ["collections.library_id", "collections.collection_id"],
            ondelete="CASCADE",
        ),
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT, INSERT, UPDATE ON zotero_import_sources,
                    zotero_import_entries, zotero_collection_mappings TO literature_worker;
                GRANT SELECT, INSERT ON collections TO literature_worker;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_table("zotero_collection_mappings")
    op.drop_table("zotero_import_entries")
    op.drop_table("zotero_import_sources")
    op.drop_constraint(
        "canonical_metadata_metadata_source_check",
        "canonical_metadata",
        type_="check",
    )
    op.create_check_constraint(
        "canonical_metadata_metadata_source_check",
        "canonical_metadata",
        "metadata_source IN ('UNDEFINED','CROSSREF','OPENALEX')",
    )
