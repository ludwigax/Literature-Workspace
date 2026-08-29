"""Create global Pipeline, Document Database, release, Document, and Chunk storage.

Revision ID: 20260908_0019
Revises: 20260907_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0019"
down_revision: str | None = "20260907_0018"
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


def json_object(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(),
        server_default=sa.text("'{}'::jsonb") if not nullable else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "document_pipelines",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('ACTIVE','ARCHIVED')"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("pipeline_id"),
    )
    op.create_table(
        "document_pipeline_versions",
        sa.Column("pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        json_object("model_config"),
        json_object("input_config"),
        sa.Column("splitter_type", sa.String(20), nullable=False),
        json_object("splitter_config"),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("splitter_type IN ('WHOLE','JSON','PARAGRAPH','MARKDOWN','ADVANCED')"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["pipeline_id"], ["document_pipelines.pipeline_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("pipeline_version_id"),
        sa.UniqueConstraint("pipeline_id", "version", name="uq_document_pipeline_version"),
        sa.UniqueConstraint("pipeline_id", "config_hash", name="uq_document_pipeline_config_hash"),
    )
    op.create_foreign_key(
        "fk_document_pipeline_active_version",
        "document_pipelines",
        "document_pipeline_versions",
        ["active_version_id"],
        ["pipeline_version_id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "document_databases",
        sa.Column("database_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("range_mode", sa.String(20), nullable=False),
        sa.Column("range_revision", sa.Integer(), nullable=False),
        sa.Column("current_release_id", sa.Uuid(), nullable=True),
        sa.Column("building_release_id", sa.Uuid(), nullable=True),
        json_object("embedding_profile"),
        json_object("bm25_profile"),
        sa.Column("retrieval_status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('ACTIVE','ARCHIVED')"),
        sa.CheckConstraint("range_mode IN ('ALL_VERIFIED','EXPLICIT')"),
        sa.CheckConstraint("retrieval_status IN ('NOT_CONFIGURED','PENDING','READY','FAILED')"),
        sa.ForeignKeyConstraint(["created_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["pipeline_id"], ["document_pipelines.pipeline_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("database_id"),
        sa.UniqueConstraint("pipeline_id", name="uq_document_database_pipeline"),
    )
    op.create_table(
        "document_database_paper_scope",
        sa.Column("database_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        *timestamps(),
        sa.ForeignKeyConstraint(["added_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["database_id"], ["document_databases.database_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("database_id", "canonical_paper_id"),
    )
    op.create_table(
        "document_database_releases",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("database_id", sa.Uuid(), nullable=False),
        sa.Column("release_number", sa.Integer(), nullable=False),
        sa.Column("pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column("range_revision", sa.Integer(), nullable=False),
        sa.Column("target_manifest_hash", sa.String(64), nullable=False),
        sa.Column("build_mode", sa.String(20), nullable=False),
        sa.Column("trigger_reason", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        json_object("completeness_report"),
        json_object("embedding_profile"),
        json_object("bm25_profile"),
        sa.Column("retrieval_status", sa.String(20), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('BUILDING','CURRENT','ARCHIVED','FAILED')"),
        sa.CheckConstraint("build_mode IN ('FULL','UPDATE')"),
        sa.CheckConstraint("retrieval_status IN ('NOT_CONFIGURED','PENDING','READY','FAILED')"),
        sa.ForeignKeyConstraint(
            ["database_id"], ["document_databases.database_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_version_id"],
            ["document_pipeline_versions.pipeline_version_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("release_id"),
        sa.UniqueConstraint("database_id", "release_number", name="uq_document_database_release"),
    )
    op.create_index(
        "ix_document_release_manifest",
        "document_database_releases",
        ["database_id", "target_manifest_hash"],
    )
    op.create_index(
        "uq_document_database_current_release",
        "document_database_releases",
        ["database_id"],
        unique=True,
        postgresql_where=sa.text("status = 'CURRENT'"),
    )
    op.create_index(
        "uq_document_database_building_release",
        "document_database_releases",
        ["database_id"],
        unique=True,
        postgresql_where=sa.text("status = 'BUILDING'"),
    )
    op.create_foreign_key(
        "fk_document_database_current_release",
        "document_databases",
        "document_database_releases",
        ["current_release_id"],
        ["release_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_document_database_building_release",
        "document_databases",
        "document_database_releases",
        ["building_release_id"],
        ["release_id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "pipeline_documents",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("splitter_config_hash", sa.String(64), nullable=False),
        sa.Column("content_blob_id", sa.Uuid(), nullable=False),
        sa.Column("raw_output_blob_id", sa.Uuid(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("word_count", sa.BigInteger(), nullable=False),
        json_object("provenance"),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["content_blob_id"], ["blobs.blob_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["pipeline_version_id"],
            ["document_pipeline_versions.pipeline_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["raw_output_blob_id"], ["blobs.blob_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"], ["artifacts.artifact_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_index(
        "ix_pipeline_document_reuse",
        "pipeline_documents",
        ["canonical_paper_id", "pipeline_version_id", "source_fingerprint", "splitter_config_hash"],
    )
    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("facet_1", sa.String(500), nullable=True),
        sa.Column("facet_2", sa.String(500), nullable=True),
        json_object("attributes"),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["pipeline_documents.document_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_document_chunk_ordinal"),
    )
    op.create_index("ix_document_chunk_paper", "document_chunks", ["canonical_paper_id"])
    op.create_index("ix_document_chunk_facets", "document_chunks", ["facet_1", "facet_2"])
    op.create_table(
        "document_release_items",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        json_object("error", nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','REUSED','SUCCEEDED','FAILED')"),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["pipeline_documents.document_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["document_database_releases.release_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"], ["artifacts.artifact_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("release_id", "canonical_paper_id"),
    )
    op.create_index("ix_document_release_item_document", "document_release_items", ["document_id"])

    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT ON document_pipelines, document_pipeline_versions,
                    document_databases, document_database_paper_scope,
                    document_database_releases, pipeline_documents,
                    document_chunks, document_release_items TO literature_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON document_pipelines,
                    document_pipeline_versions, document_databases,
                    document_database_paper_scope, document_database_releases,
                    pipeline_documents, document_chunks, document_release_items
                    TO literature_worker;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_table("document_release_items")
    op.drop_table("document_chunks")
    op.drop_table("pipeline_documents")
    op.drop_constraint(
        "fk_document_database_building_release", "document_databases", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_document_database_current_release", "document_databases", type_="foreignkey"
    )
    op.drop_table("document_database_releases")
    op.drop_table("document_database_paper_scope")
    op.drop_table("document_databases")
    op.drop_constraint(
        "fk_document_pipeline_active_version", "document_pipelines", type_="foreignkey"
    )
    op.drop_table("document_pipeline_versions")
    op.drop_table("document_pipelines")
