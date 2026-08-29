"""Add the Document-specific durable build queue.

Revision ID: 20260912_0023
Revises: 20260911_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260912_0023"
down_revision: str | None = "20260911_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "document_build_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("database_id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=True),
        sa.Column("pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column("range_revision", sa.Integer(), nullable=False),
        sa.Column("build_mode", sa.String(20), nullable=False),
        sa.Column("trigger_reason", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("reconcile_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')"),
        sa.CheckConstraint(
            "phase IN ('SOURCE_PREPARATION','DOCUMENTS','MANIFEST','EMBEDDINGS',"
            "'VALIDATION','PUBLISH','COMPLETED')"
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"], ["principals.principal_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["database_id"], ["document_databases.database_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_version_id"],
            ["document_pipeline_versions.pipeline_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["document_database_releases.release_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("release_id"),
    )
    op.create_index(
        "uq_document_database_running_build",
        "document_build_runs",
        ["database_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "document_build_tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("queue_name", sa.String(80), nullable=False),
        sa.Column("subject_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("progress_message", sa.String(500), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')"),
        sa.ForeignKeyConstraint(["run_id"], ["document_build_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint(
            "run_id", "task_type", "subject_key", name="uq_document_build_task_identity"
        ),
    )
    op.create_index(
        "ix_document_build_task_claim",
        "document_build_tasks",
        ["queue_name", "status", "available_at"],
    )
    op.create_table(
        "document_build_task_attempts",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["document_build_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_document_build_task_attempt"),
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT ON document_build_runs, document_build_tasks,
                    document_build_task_attempts TO literature_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON document_build_runs,
                    document_build_tasks, document_build_task_attempts TO literature_worker;
                GRANT SELECT, INSERT, UPDATE ON blobs, artifacts TO literature_worker;
                GRANT SELECT, UPDATE ON canonical_papers TO literature_worker;
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
                REVOKE INSERT, UPDATE ON blobs FROM literature_worker;
                REVOKE INSERT, UPDATE ON artifacts FROM literature_worker;
            END IF;
        END
        $revoke$;
        """
    )
    op.drop_table("document_build_task_attempts")
    op.drop_index("ix_document_build_task_claim", table_name="document_build_tasks")
    op.drop_table("document_build_tasks")
    op.drop_index("uq_document_database_running_build", table_name="document_build_runs")
    op.drop_table("document_build_runs")
