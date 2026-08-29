"""Extend the worker role for citation ingestion commands.

Revision ID: 20260901_0012
Revises: 20260831_0011
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260901_0012"
down_revision: str | None = "20260831_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT ON principals, library_memberships, blobs TO literature_worker;
                GRANT SELECT, INSERT ON canonical_papers, canonical_identifiers,
                    canonical_metadata, library_items TO literature_worker;
                GRANT SELECT, INSERT, UPDATE ON background_jobs TO literature_worker;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $revoke$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                REVOKE INSERT ON canonical_papers, canonical_identifiers,
                    canonical_metadata, library_items, background_jobs FROM literature_worker;
                REVOKE SELECT ON principals, library_memberships, blobs FROM literature_worker;
            END IF;
        END
        $revoke$;
        """
    )
