"""Allow the worker to read values returned by outbox inserts.

Revision ID: 20260902_0013
Revises: 20260901_0012
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260902_0013"
down_revision: str | None = "20260901_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT ON outbox_events TO literature_worker;
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
                REVOKE SELECT ON outbox_events FROM literature_worker;
            END IF;
        END
        $revoke$;
        """
    )
