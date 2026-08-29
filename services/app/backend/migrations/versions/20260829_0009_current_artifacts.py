"""Replace versioned Assets with current Artifacts and ordinary Assets.

Revision ID: 20260829_0009
Revises: 20260828_0008

This is a pre-public M3 contract correction. The former Asset tables had no
public API or accepted upload data, so their provisional rows are intentionally
discarded rather than assigned ambiguous Artifact semantics.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0009"
down_revision: str | None = "20260828_0008"
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
    op.drop_table("item_assets")
    op.drop_index("ix_assets_library_family", table_name="assets")
    op.drop_table("assets")

    op.create_unique_constraint(
        "uq_library_item_paper_scope",
        "library_items",
        ["library_id", "library_item_id", "canonical_paper_id"],
    )

    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_key", sa.String(length=255), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("original_filename", sa.String(length=1024), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint(
            "artifact_type IN "
            "('SOURCE_PDF','EXTRACTED_TEXT','SUPPLEMENT','PIPELINE_DOCUMENT')"
        ),
        sa.CheckConstraint("status IN ('ACTIVE','STALE')"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.blob_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint(
            "canonical_paper_id", "artifact_key", name="uq_artifact_paper_key"
        ),
    )
    op.create_index(
        "ix_artifacts_paper_type", "artifacts", ["canonical_paper_id", "artifact_type"]
    )

    op.create_table(
        "item_artifact_overrides",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_key", sa.String(length=255), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=1024), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("specified_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint(
            "artifact_type IN "
            "('SOURCE_PDF','EXTRACTED_TEXT','SUPPLEMENT','PIPELINE_DOCUMENT')"
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.blob_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["library_id", "library_item_id", "canonical_paper_id"],
            [
                "library_items.library_id",
                "library_items.library_item_id",
                "library_items.canonical_paper_id",
            ],
            ondelete="CASCADE",
            name="fk_item_artifact_override_item_paper",
        ),
        sa.ForeignKeyConstraint(
            ["specified_by"], ["principals.principal_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("library_id", "library_item_id", "artifact_key"),
    )
    op.create_index(
        "ix_item_artifact_overrides_blob", "item_artifact_overrides", ["blob_id"]
    )

    op.create_table(
        "assets",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('ACTIVE','DELETED')"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.blob_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_asset_item_scope",
        ),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    op.create_index(
        "ix_assets_library_item",
        "assets",
        ["library_id", "library_item_id", "created_at"],
    )

    op.execute(
        """
        ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
        ALTER TABLE item_artifact_overrides ENABLE ROW LEVEL SECURITY;

        CREATE POLICY assets_select ON assets FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY assets_insert ON assets FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY assets_update ON assets FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY assets_delete ON assets FOR DELETE
            USING (app_security.can_edit_library(library_id));

        CREATE POLICY artifact_overrides_select ON item_artifact_overrides FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY artifact_overrides_insert ON item_artifact_overrides FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY artifact_overrides_update ON item_artifact_overrides FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY artifact_overrides_delete ON item_artifact_overrides FOR DELETE
            USING (app_security.can_edit_library(library_id));
        """
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT, INSERT, UPDATE ON artifacts TO literature_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON assets,
                    item_artifact_overrides TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260829_0009 is an intentional pre-public contract correction; "
        "restore from backup instead of recreating the discarded provisional schema"
    )
