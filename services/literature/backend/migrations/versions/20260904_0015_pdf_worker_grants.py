"""Grant the worker commands needed for PDF DOI reconciliation.

Revision ID: 20260904_0015
Revises: 20260903_0014
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_0015"
down_revision: str | None = "20260903_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT UPDATE, DELETE ON library_items TO literature_worker;
                GRANT DELETE ON canonical_papers TO literature_worker;
                GRANT SELECT, DELETE ON artifacts TO literature_worker;
                GRANT SELECT, INSERT, UPDATE, DELETE ON item_artifact_overrides
                    TO literature_worker;
                GRANT SELECT, INSERT, UPDATE ON assets TO literature_worker;
                GRANT SELECT, INSERT ON collection_items, item_tags TO literature_worker;
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
                REVOKE UPDATE, DELETE ON library_items FROM literature_worker;
                REVOKE DELETE ON canonical_papers FROM literature_worker;
                REVOKE SELECT, DELETE ON artifacts FROM literature_worker;
                REVOKE SELECT, INSERT, UPDATE, DELETE ON item_artifact_overrides
                    FROM literature_worker;
                REVOKE SELECT, INSERT, UPDATE ON assets FROM literature_worker;
                REVOKE SELECT, INSERT ON collection_items, item_tags FROM literature_worker;
            END IF;
        END
        $revoke$;
        """
    )
