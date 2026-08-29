"""Add Release-local manifests, facet bitmaps, and BM25 chunk caches.

Revision ID: 20260910_0021
Revises: 20260909_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0021"
down_revision: str | None = "20260909_0020"
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


def json_object(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "document_release_indexes",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("bm25_status", sa.String(20), nullable=False),
        sa.Column("bm25_analyzer_hash", sa.String(64), nullable=False),
        sa.Column("bm25_document_count", sa.Integer(), nullable=False),
        sa.Column("bm25_average_document_length", sa.Float(), nullable=False),
        json_object("bm25_document_frequencies"),
        json_object("bm25_inverse_document_frequencies"),
        sa.Column("embedding_status", sa.String(20), nullable=False),
        sa.Column("embedding_profile_hash", sa.String(64), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('BUILDING','READY','FAILED')"),
        sa.CheckConstraint("bm25_status IN ('BUILDING','READY','FAILED')"),
        sa.CheckConstraint(
            "embedding_status IN ('NOT_CONFIGURED','BUILDING','READY','FAILED')"
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["document_database_releases.release_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("release_id"),
    )
    op.create_table(
        "document_index_manifest_rows",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("facet_1", sa.String(500), nullable=True),
        sa.Column("facet_2", sa.String(500), nullable=True),
        sa.Column("bm25_analyzer_hash", sa.String(64), nullable=False),
        sa.Column("bm25_document_length", sa.Integer(), nullable=False),
        json_object("bm25_term_frequencies"),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["document_chunks.chunk_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["document_release_indexes.release_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("release_id", "row_number"),
        sa.UniqueConstraint(
            "release_id", "chunk_id", name="uq_document_index_manifest_chunk"
        ),
    )
    op.create_index(
        "ix_document_index_manifest_facets",
        "document_index_manifest_rows",
        ["release_id", "facet_1", "facet_2"],
    )
    op.create_table(
        "document_index_facet_bitmaps",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("facet_slot", sa.Integer(), nullable=False),
        sa.Column("facet_value", sa.String(500), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("bitmap", sa.LargeBinary(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("facet_slot IN (1, 2)"),
        sa.ForeignKeyConstraint(
            ["release_id"], ["document_release_indexes.release_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("release_id", "facet_slot", "facet_value"),
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT ON document_release_indexes, document_index_manifest_rows,
                    document_index_facet_bitmaps TO literature_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON document_release_indexes,
                    document_index_manifest_rows, document_index_facet_bitmaps
                    TO literature_worker;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_table("document_index_facet_bitmaps")
    op.drop_index(
        "ix_document_index_manifest_facets", table_name="document_index_manifest_rows"
    )
    op.drop_table("document_index_manifest_rows")
    op.drop_table("document_release_indexes")
