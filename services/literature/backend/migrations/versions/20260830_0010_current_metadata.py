"""Replace canonical metadata versions with one current metadata row.

Revision ID: 20260830_0010
Revises: 20260829_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_0010"
down_revision: str | None = "20260829_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "canonical_metadata",
        sa.Column("canonical_paper_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_source", sa.String(length=20), nullable=False),
        sa.Column("source_record_id", sa.String(length=500), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("publication_year", sa.SmallInteger(), nullable=True),
        sa.Column("publication_month", sa.SmallInteger(), nullable=True),
        sa.Column("publication_day", sa.SmallInteger(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("publication_date_precision", sa.String(length=10), nullable=True),
        sa.Column("work_type", sa.String(length=50), nullable=True),
        sa.Column("venue", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("volume", sa.String(length=200), nullable=True),
        sa.Column("issue", sa.String(length=200), nullable=True),
        sa.Column("pages", sa.String(length=200), nullable=True),
        sa.Column("article_number", sa.String(length=200), nullable=True),
        sa.Column("language", sa.String(length=100), nullable=True),
        sa.Column("issn", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("isbn", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column(
            "authors",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "extra",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("metadata_source IN ('UNDEFINED','CROSSREF','OPENALEX')"),
        sa.CheckConstraint("publication_year IS NULL OR publication_year BETWEEN 1000 AND 3000"),
        sa.CheckConstraint("publication_month IS NULL OR publication_month BETWEEN 1 AND 12"),
        sa.CheckConstraint("publication_day IS NULL OR publication_day BETWEEN 1 AND 31"),
        sa.CheckConstraint(
            "publication_date_precision IS NULL OR publication_date_precision IN ('YEAR','MONTH','DAY')"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_paper_id"], ["canonical_papers.canonical_paper_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("canonical_paper_id"),
    )
    op.execute(
        """
        INSERT INTO canonical_metadata (
            canonical_paper_id, metadata_source, source_record_id, title,
            abstract, publication_year, publication_date, venue, authors,
            extra, provenance, revision, updated_by, created_at, updated_at
        )
        SELECT
            paper.canonical_paper_id, 'UNDEFINED', NULL, metadata.title,
            metadata.abstract, metadata.publication_year, metadata.publication_date,
            metadata.venue, metadata.authors, metadata.extra, metadata.provenance,
            1, metadata.created_by, metadata.created_at, metadata.created_at
        FROM canonical_papers AS paper
        JOIN canonical_metadata_versions AS metadata
          ON metadata.metadata_version_id = paper.current_metadata_version_id
        """
    )
    op.drop_constraint(
        "fk_canonical_current_metadata",
        "canonical_papers",
        type_="foreignkey",
    )
    op.drop_column("canonical_papers", "current_metadata_version_id")
    op.drop_table("canonical_metadata_versions")

    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT, INSERT, UPDATE ON canonical_metadata TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260830_0010 intentionally removes product-visible metadata history; "
        "restore from backup instead of fabricating historical rows"
    )
