"""Add system roles and Document reconcile policy.

Revision ID: 20260913_0024
Revises: 20260912_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260913_0024"
down_revision: str | None = "20260912_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principal_system_roles",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('ADMIN','USER')"),
        sa.ForeignKeyConstraint(["assigned_by"], ["principals.principal_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.principal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("principal_id"),
    )
    op.execute(
        """
        INSERT INTO principal_system_roles (principal_id, role)
        SELECT principal_id, 'USER' FROM principals
        ON CONFLICT (principal_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE principal_system_roles AS roles
        SET role = 'ADMIN', updated_at = now()
        FROM external_identities AS identities
        WHERE identities.principal_id = roles.principal_id
          AND lower(identities.email) = 'alice@example.test'
        """
    )
    op.add_column(
        "document_databases",
        sa.Column(
            "auto_reconcile_enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "document_databases",
        sa.Column("last_reconcile_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_databases",
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON principal_system_roles
                    TO literature_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON document_pipelines,
                    document_pipeline_versions, document_databases,
                    document_database_paper_scope, document_database_releases,
                    document_build_runs, document_build_tasks,
                    document_build_task_attempts TO literature_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT ON principal_system_roles TO literature_worker;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.drop_column("document_databases", "next_reconcile_at")
    op.drop_column("document_databases", "last_reconcile_checked_at")
    op.drop_column("document_databases", "auto_reconcile_enabled")
    op.drop_table("principal_system_roles")
