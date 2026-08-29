"""Grant the dedicated worker its narrow cross-tenant processing privileges.

Revision ID: 20260831_0011
Revises: 20260830_0010
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260831_0011"
down_revision: str | None = "20260830_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT USAGE ON SCHEMA public TO literature_worker;
                GRANT SELECT ON libraries, library_items, canonical_papers,
                    canonical_identifiers, canonical_metadata TO literature_worker;
                GRANT SELECT, UPDATE ON background_jobs, job_attempts TO literature_worker;
                GRANT UPDATE ON canonical_metadata TO literature_worker;
                GRANT INSERT ON job_attempts, outbox_events TO literature_worker;
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
                REVOKE ALL ON libraries, library_items, canonical_papers,
                    canonical_identifiers, canonical_metadata, background_jobs,
                    job_attempts, outbox_events FROM literature_worker;
            END IF;
        END
        $revoke$;
        """
    )
