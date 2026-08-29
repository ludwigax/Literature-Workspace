"""Add canonical PDF verification and verified Document ranges.

Revision ID: 20260914_0025
Revises: 20260913_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0025"
down_revision: str | None = "20260913_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("verification_status", sa.String(20), nullable=True))
    op.execute(
        """
        UPDATE artifacts
        SET verification_status = CASE
            WHEN upper(provenance ->> 'verification_status') = 'VERIFIED' THEN 'VERIFIED'
            ELSE 'UNVERIFIED'
        END
        WHERE artifact_type = 'SOURCE_PDF'
        """
    )
    op.create_check_constraint(
        "ck_artifact_verification_status",
        "artifacts",
        "verification_status IS NULL OR verification_status IN ('UNVERIFIED','VERIFIED')",
    )
    op.create_check_constraint(
        "ck_artifact_verification_pdf_only",
        "artifacts",
        "artifact_type = 'SOURCE_PDF' OR verification_status IS NULL",
    )
    op.create_index(
        "ix_artifacts_verified_pdf",
        "artifacts",
        ["canonical_paper_id"],
        unique=False,
        postgresql_where=sa.text(
            "artifact_type = 'SOURCE_PDF' AND status = 'ACTIVE' "
            "AND verification_status = 'VERIFIED'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_verified_pdf", table_name="artifacts")
    op.drop_constraint("ck_artifact_verification_pdf_only", "artifacts", type_="check")
    op.drop_constraint("ck_artifact_verification_status", "artifacts", type_="check")
    op.drop_column("artifacts", "verification_status")
