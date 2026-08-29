"""Prepare current Document release projection into Library Artifacts.

Revision ID: 20260909_0020
Revises: 20260908_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0020"
down_revision: str | None = "20260908_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("document_release_items", "document_release_entries")
    op.execute(
        "ALTER INDEX ix_document_release_item_document RENAME TO ix_document_release_entry_document"
    )
    op.create_index(
        "ix_document_release_entry_paper",
        "document_release_entries",
        ["canonical_paper_id", "release_id"],
    )
    op.add_column(
        "pipeline_documents",
        sa.Column("display_title", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "pipeline_documents",
        sa.Column(
            "media_type",
            sa.String(length=255),
            server_default="text/markdown",
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE pipeline_documents AS document
        SET display_title = pipeline.name
        FROM document_pipeline_versions AS version
        JOIN document_pipelines AS pipeline
          ON pipeline.pipeline_id = version.pipeline_id
        WHERE document.pipeline_version_id = version.pipeline_version_id
          AND document.display_title IS NULL
        """
    )
    op.alter_column("pipeline_documents", "display_title", nullable=False)


def downgrade() -> None:
    op.drop_column("pipeline_documents", "media_type")
    op.drop_column("pipeline_documents", "display_title")
    op.drop_index(
        "ix_document_release_entry_paper",
        table_name="document_release_entries",
    )
    op.execute(
        "ALTER INDEX ix_document_release_entry_document RENAME TO ix_document_release_item_document"
    )
    op.rename_table("document_release_entries", "document_release_items")
