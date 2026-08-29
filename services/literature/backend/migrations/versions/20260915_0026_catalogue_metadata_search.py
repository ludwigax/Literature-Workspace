"""Add trigram indexes for Library bibliographic metadata search.

Revision ID: 20260915_0026
Revises: 20260914_0025
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0026"
down_revision: str | None = "20260914_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_canonical_metadata_title_trgm "
        "ON canonical_metadata USING gin (lower(title) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_metadata_authors_trgm "
        "ON canonical_metadata USING gin ((lower(authors::text)) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_metadata_venue_trgm "
        "ON canonical_metadata USING gin (lower(venue) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_metadata_publisher_trgm "
        "ON canonical_metadata USING gin (lower(publisher) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_library_items_overrides_trgm "
        "ON library_items USING gin ((lower(local_overrides::text)) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_identifiers_normalized_trgm "
        "ON canonical_identifiers USING gin (lower(normalized_value) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_identifiers_original_trgm "
        "ON canonical_identifiers USING gin (lower(original_value) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_canonical_identifiers_original_trgm", table_name="canonical_identifiers"
    )
    op.drop_index(
        "ix_canonical_identifiers_normalized_trgm", table_name="canonical_identifiers"
    )
    op.drop_index("ix_library_items_overrides_trgm", table_name="library_items")
    op.drop_index("ix_canonical_metadata_publisher_trgm", table_name="canonical_metadata")
    op.drop_index("ix_canonical_metadata_venue_trgm", table_name="canonical_metadata")
    op.drop_index("ix_canonical_metadata_authors_trgm", table_name="canonical_metadata")
    op.drop_index("ix_canonical_metadata_title_trgm", table_name="canonical_metadata")
