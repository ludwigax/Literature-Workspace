"""Allow the worker to lock canonical Papers during metadata replacement.

Revision ID: 20260903_0014
Revises: 20260902_0013
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260903_0014"
down_revision: str | None = "20260902_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT UPDATE ON canonical_papers TO literature_worker;
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
                REVOKE UPDATE ON canonical_papers FROM literature_worker;
            END IF;
        END
        $revoke$;
        """
    )
