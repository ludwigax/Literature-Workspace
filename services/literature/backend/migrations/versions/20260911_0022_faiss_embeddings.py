"""Add persisted FAISS metadata to Release indexes.

Revision ID: 20260911_0022
Revises: 20260910_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911_0022"
down_revision: str | None = "20260910_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_release_indexes", sa.Column("embedding_model", sa.String(255))
    )
    op.add_column(
        "document_release_indexes", sa.Column("embedding_dimensions", sa.Integer())
    )
    op.add_column(
        "document_release_indexes", sa.Column("embedding_metric", sa.String(30))
    )
    op.add_column(
        "document_release_indexes", sa.Column("embedding_index_type", sa.String(50))
    )
    op.add_column(
        "document_release_indexes", sa.Column("embedding_index_blob_id", sa.Uuid())
    )
    op.add_column(
        "document_release_indexes", sa.Column("embedding_index_sha256", sa.String(64))
    )
    op.create_foreign_key(
        "fk_document_release_index_embedding_blob",
        "document_release_indexes",
        "blobs",
        ["embedding_index_blob_id"],
        ["blob_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_document_release_index_embedding_blob",
        "document_release_indexes",
        type_="foreignkey",
    )
    op.drop_column("document_release_indexes", "embedding_index_sha256")
    op.drop_column("document_release_indexes", "embedding_index_blob_id")
    op.drop_column("document_release_indexes", "embedding_index_type")
    op.drop_column("document_release_indexes", "embedding_metric")
    op.drop_column("document_release_indexes", "embedding_dimensions")
    op.drop_column("document_release_indexes", "embedding_model")
