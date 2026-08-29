from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .catalogue import json_type


class DocumentPipeline(TimestampMixin, Base):
    """An administrator-defined Paper -> Document recipe."""

    __tablename__ = "document_pipelines"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','ARCHIVED')"),)

    pipeline_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "document_pipeline_versions.pipeline_version_id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_document_pipeline_active_version",
        )
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class DocumentPipelineVersion(TimestampMixin, Base):
    """An immutable recipe snapshot. Updating a Pipeline creates another row."""

    __tablename__ = "document_pipeline_versions"
    __table_args__ = (
        CheckConstraint("splitter_type IN ('WHOLE','JSON','PARAGRAPH','MARKDOWN','ADVANCED')"),
        UniqueConstraint("pipeline_id", "version", name="uq_document_pipeline_version"),
        UniqueConstraint("pipeline_id", "config_hash", name="uq_document_pipeline_config_hash"),
    )

    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pipelines.pipeline_id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    model_config: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    input_config: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    splitter_type: Mapped[str] = mapped_column(String(20), nullable=False)
    splitter_config: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class DocumentDatabase(TimestampMixin, Base):
    """A global, Pipeline-oriented corpus; it is deliberately not Library-owned."""

    __tablename__ = "document_databases"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','ARCHIVED')"),
        CheckConstraint("range_mode IN ('ALL_VERIFIED','EXPLICIT')"),
        CheckConstraint("retrieval_status IN ('NOT_CONFIGURED','PENDING','READY','FAILED')"),
        UniqueConstraint("pipeline_id", name="uq_document_database_pipeline"),
    )

    database_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pipelines.pipeline_id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    range_mode: Mapped[str] = mapped_column(String(20), default="EXPLICIT", nullable=False)
    range_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_release_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "document_database_releases.release_id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_document_database_current_release",
        )
    )
    building_release_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "document_database_releases.release_id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_document_database_building_release",
        )
    )
    embedding_profile: Mapped[dict[str, Any]] = mapped_column(
        json_type, default=dict, nullable=False
    )
    bm25_profile: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    retrieval_status: Mapped[str] = mapped_column(
        String(20), default="NOT_CONFIGURED", nullable=False
    )
    auto_reconcile_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_reconcile_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class DocumentDatabasePaperScope(TimestampMixin, Base):
    """The desired Paper range when a Database uses EXPLICIT range mode."""

    __tablename__ = "document_database_paper_scope"

    database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_databases.database_id", ondelete="CASCADE"), primary_key=True
    )
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="RESTRICT"), primary_key=True
    )
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class DocumentDatabaseRelease(TimestampMixin, Base):
    """One immutable corpus snapshot, BUILDING until atomically published."""

    __tablename__ = "document_database_releases"
    __table_args__ = (
        CheckConstraint("status IN ('BUILDING','CURRENT','ARCHIVED','FAILED')"),
        CheckConstraint("build_mode IN ('FULL','UPDATE')"),
        CheckConstraint("retrieval_status IN ('NOT_CONFIGURED','PENDING','READY','FAILED')"),
        UniqueConstraint("database_id", "release_number", name="uq_document_database_release"),
        Index("ix_document_release_manifest", "database_id", "target_manifest_hash"),
        Index(
            "uq_document_database_current_release",
            "database_id",
            unique=True,
            postgresql_where=text("status = 'CURRENT'"),
        ),
        Index(
            "uq_document_database_building_release",
            "database_id",
            unique=True,
            postgresql_where=text("status = 'BUILDING'"),
        ),
    )

    release_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_databases.database_id", ondelete="CASCADE"), nullable=False
    )
    release_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pipeline_versions.pipeline_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    range_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    build_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="BUILDING", nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completeness_report: Mapped[dict[str, Any]] = mapped_column(
        json_type, default=dict, nullable=False
    )
    embedding_profile: Mapped[dict[str, Any]] = mapped_column(
        json_type, default=dict, nullable=False
    )
    bm25_profile: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    retrieval_status: Mapped[str] = mapped_column(
        String(20), default="NOT_CONFIGURED", nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineDocument(TimestampMixin, Base):
    """An immutable Pipeline result reusable by multiple Database releases."""

    __tablename__ = "pipeline_documents"
    __table_args__ = (
        Index(
            "ix_pipeline_document_reuse",
            "canonical_paper_id",
            "pipeline_version_id",
            "source_fingerprint",
            "splitter_config_hash",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="RESTRICT"), nullable=False
    )
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pipeline_versions.pipeline_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="SET NULL")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    splitter_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.blob_id", ondelete="RESTRICT"), nullable=False
    )
    raw_output_blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.blob_id", ondelete="RESTRICT"), nullable=False
    )
    display_title: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), default="text/markdown", nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    word_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)


class DocumentChunk(TimestampMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunk_ordinal"),
        Index("ix_document_chunk_paper", "canonical_paper_id"),
        Index("ix_document_chunk_facets", "facet_1", "facet_2"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_documents.document_id", ondelete="CASCADE"), nullable=False
    )
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    facet_1: Mapped[str | None] = mapped_column(String(500))
    facet_2: Mapped[str | None] = mapped_column(String(500))
    attributes: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)


class DocumentReleaseEntry(TimestampMixin, Base):
    """A Paper slot in a Release and its referenced immutable Document."""

    __tablename__ = "document_release_entries"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','REUSED','SUCCEEDED','FAILED')"),
        Index("ix_document_release_entry_document", "document_id"),
        Index(
            "ix_document_release_entry_paper",
            "canonical_paper_id",
            "release_id",
        ),
    )

    release_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_database_releases.release_id", ondelete="CASCADE"), primary_key=True
    )
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="RESTRICT"), primary_key=True
    )
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="SET NULL")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_documents.document_id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(json_type)


class DocumentReleaseIndex(TimestampMixin, Base):
    """The disposable retrieval index owned by one current or building Release."""

    __tablename__ = "document_release_indexes"
    __table_args__ = (
        CheckConstraint("status IN ('BUILDING','READY','FAILED')"),
        CheckConstraint("bm25_status IN ('BUILDING','READY','FAILED')"),
        CheckConstraint("embedding_status IN ('NOT_CONFIGURED','BUILDING','READY','FAILED')"),
    )

    release_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_database_releases.release_id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="BUILDING", nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bm25_status: Mapped[str] = mapped_column(String(20), default="BUILDING", nullable=False)
    bm25_analyzer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bm25_document_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bm25_average_document_length: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bm25_document_frequencies: Mapped[dict[str, int]] = mapped_column(
        json_type, default=dict, nullable=False
    )
    bm25_inverse_document_frequencies: Mapped[dict[str, float]] = mapped_column(
        json_type, default=dict, nullable=False
    )
    embedding_status: Mapped[str] = mapped_column(
        String(20), default="NOT_CONFIGURED", nullable=False
    )
    embedding_profile_hash: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    embedding_metric: Mapped[str | None] = mapped_column(String(30))
    embedding_index_type: Mapped[str | None] = mapped_column(String(50))
    embedding_index_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.blob_id", ondelete="RESTRICT")
    )
    embedding_index_sha256: Mapped[str | None] = mapped_column(String(64))


class DocumentIndexManifestRow(TimestampMixin, Base):
    """Release-local dense row mapping plus disposable retrieval caches."""

    __tablename__ = "document_index_manifest_rows"
    __table_args__ = (
        UniqueConstraint("release_id", "chunk_id", name="uq_document_index_manifest_chunk"),
        Index("ix_document_index_manifest_facets", "release_id", "facet_1", "facet_2"),
    )

    release_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_release_indexes.release_id", ondelete="CASCADE"),
        primary_key=True,
    )
    row_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_chunks.chunk_id", ondelete="RESTRICT"), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    facet_1: Mapped[str | None] = mapped_column(String(500))
    facet_2: Mapped[str | None] = mapped_column(String(500))
    bm25_analyzer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bm25_document_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bm25_term_frequencies: Mapped[dict[str, int]] = mapped_column(
        json_type, default=dict, nullable=False
    )


class DocumentIndexFacetBitmap(TimestampMixin, Base):
    """A derived row bitmap for one facet value; DocumentChunk remains authoritative."""

    __tablename__ = "document_index_facet_bitmaps"
    __table_args__ = (CheckConstraint("facet_slot IN (1, 2)"),)

    release_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_release_indexes.release_id", ondelete="CASCADE"),
        primary_key=True,
    )
    facet_slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    facet_value: Mapped[str] = mapped_column(String(500), primary_key=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    bitmap: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class DocumentBuildRun(TimestampMixin, Base):
    """Durable fixed-stage orchestration for one Database reconciliation."""

    __tablename__ = "document_build_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')"),
        CheckConstraint(
            "phase IN ('SOURCE_PREPARATION','DOCUMENTS','MANIFEST','EMBEDDINGS',"
            "'VALIDATION','PUBLISH','COMPLETED')"
        ),
        Index(
            "uq_document_database_running_build",
            "database_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_databases.database_id", ondelete="CASCADE"), nullable=False
    )
    release_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_database_releases.release_id", ondelete="SET NULL"), unique=True
    )
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pipeline_versions.pipeline_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    range_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    build_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", nullable=False)
    phase: Mapped[str] = mapped_column(String(30), default="SOURCE_PREPARATION", nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    error: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    reconcile_requested: Mapped[bool] = mapped_column(default=False, nullable=False)
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentBuildTask(TimestampMixin, Base):
    """One leased command in a Document BuildRun; domain state remains authoritative."""

    __tablename__ = "document_build_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')"),
        UniqueConstraint(
            "run_id", "task_type", "subject_key", name="uq_document_build_task_identity"
        ),
        Index("ix_document_build_task_claim", "queue_name", "status", "available_at"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_build_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    error: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    progress_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    progress_message: Mapped[str | None] = mapped_column(String(500))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentBuildTaskAttempt(Base):
    __tablename__ = "document_build_task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_document_build_task_attempt"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_build_tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(30))
    error: Mapped[dict[str, Any] | None] = mapped_column(json_type)
