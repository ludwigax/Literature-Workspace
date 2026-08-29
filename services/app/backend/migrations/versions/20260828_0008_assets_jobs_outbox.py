"""Create M3 Blob, Asset, durable job, and outbox foundations.

Revision ID: 20260828_0008
Revises: 20260827_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260828_0008"
down_revision: str | None = "20260827_0007"
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
        "blobs",
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('STAGING','AVAILABLE','QUARANTINED','DELETED')"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("blob_id"),
        sa.UniqueConstraint("sha256", name="uq_blob_sha256"),
        sa.UniqueConstraint("storage_bucket", "storage_key", name="uq_blob_storage_location"),
    )
    op.create_table(
        "assets",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("asset_family_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("asset_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("original_filename", sa.String(length=1024), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("asset_type IN ('SOURCE_PDF','EXTRACTED_TEXT','SUPPLEMENT')"),
        sa.CheckConstraint("status IN ('ACTIVE','SUPERSEDED','DELETED')"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.blob_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("library_id", "asset_id", name="uq_asset_scope"),
        sa.UniqueConstraint(
            "library_id", "asset_family_id", "version", name="uq_asset_family_version"
        ),
    )
    op.create_index(
        "ix_assets_library_family", "assets", ["library_id", "asset_family_id", "version"]
    )
    op.create_table(
        "item_assets",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attached_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('SOURCE_PDF','EXTRACTED_TEXT','SUPPLEMENT')"),
        sa.ForeignKeyConstraint(["attached_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["library_id", "asset_id"],
            ["assets.library_id", "assets.asset_id"],
            ondelete="CASCADE",
            name="fk_item_asset_asset_scope",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_item_asset_item_scope",
        ),
        sa.PrimaryKeyConstraint("library_id", "library_item_id", "asset_id", "role"),
    )
    op.create_index(
        "uq_current_item_asset_role",
        "item_assets",
        ["library_id", "library_item_id", "role"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "background_jobs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("progress_message", sa.String(length=500), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')"
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"], ["principals.principal_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("library_id", "job_id", name="uq_background_job_scope"),
    )
    op.create_index(
        "ix_jobs_claim", "background_jobs", ["status", "available_at", "lease_expires_at"]
    )
    op.create_index(
        "uq_job_idempotency",
        "background_jobs",
        ["library_id", "job_type", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "job_attempts",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["library_id", "job_id"],
            ["background_jobs.library_id", "background_jobs.job_id"],
            ondelete="CASCADE",
            name="fk_job_attempt_job_scope",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('PENDING','PUBLISHED','FAILED')"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_outbox_claim", "outbox_events", ["status", "available_at", "lease_expires_at"]
    )

    op.execute(
        """
        ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
        ALTER TABLE item_assets ENABLE ROW LEVEL SECURITY;
        ALTER TABLE background_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE job_attempts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;

        CREATE POLICY assets_select ON assets FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY assets_insert ON assets FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY assets_update ON assets FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));

        CREATE POLICY item_assets_select ON item_assets FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY item_assets_insert ON item_assets FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY item_assets_update ON item_assets FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY item_assets_delete ON item_assets FOR DELETE
            USING (app_security.can_edit_library(library_id));

        CREATE POLICY jobs_select ON background_jobs FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY jobs_insert ON background_jobs FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY jobs_update ON background_jobs FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));

        CREATE POLICY job_attempts_select ON job_attempts FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY job_attempts_insert ON job_attempts FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY job_attempts_update ON job_attempts FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));

        CREATE POLICY outbox_select ON outbox_events FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY outbox_insert ON outbox_events FOR INSERT
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY outbox_update ON outbox_events FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));
        """
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT, INSERT, UPDATE ON blobs TO literature_app;
                GRANT SELECT, INSERT, UPDATE ON assets, background_jobs, job_attempts,
                    outbox_events TO literature_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON item_assets TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_claim", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("job_attempts")
    op.drop_index("uq_job_idempotency", table_name="background_jobs")
    op.drop_index("ix_jobs_claim", table_name="background_jobs")
    op.drop_table("background_jobs")
    op.drop_index("uq_current_item_asset_role", table_name="item_assets")
    op.drop_table("item_assets")
    op.drop_index("ix_assets_library_family", table_name="assets")
    op.drop_table("assets")
    op.drop_table("blobs")
