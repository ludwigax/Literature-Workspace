"""Add arXiv as a current metadata source.

Revision ID: 20260906_0017
Revises: 20260905_0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260906_0017"
down_revision: str | None = "20260905_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "canonical_metadata_metadata_source_check", "canonical_metadata", type_="check"
    )
    op.create_check_constraint(
        "canonical_metadata_metadata_source_check",
        "canonical_metadata",
        "metadata_source IN ('UNDEFINED','CROSSREF','OPENALEX','ARXIV','ZOTERO')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "canonical_metadata_metadata_source_check", "canonical_metadata", type_="check"
    )
    op.create_check_constraint(
        "canonical_metadata_metadata_source_check",
        "canonical_metadata",
        "metadata_source IN ('UNDEFINED','CROSSREF','OPENALEX','ZOTERO')",
    )
