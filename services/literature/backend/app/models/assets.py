from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .catalogue import json_type


class Blob(TimestampMixin, Base):
    """One immutable physical object, shared only when its bytes are identical."""

    __tablename__ = "blobs"
    __table_args__ = (
        CheckConstraint("status IN ('STAGING','AVAILABLE','QUARANTINED','DELETED')"),
        UniqueConstraint("sha256", name="uq_blob_sha256"),
        UniqueConstraint("storage_bucket", "storage_key", name="uq_blob_storage_location"),
    )

    blob_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="STAGING", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class Artifact(TimestampMixin, Base):
    """The current canonical content for one named paper resource."""

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('SOURCE_PDF','EXTRACTED_TEXT','SUPPLEMENT','PIPELINE_DOCUMENT')"
        ),
        CheckConstraint("status IN ('ACTIVE','STALE')"),
        CheckConstraint(
            "verification_status IS NULL OR verification_status IN ('UNVERIFIED','VERIFIED')",
            name="ck_artifact_verification_status",
        ),
        CheckConstraint(
            "artifact_type = 'SOURCE_PDF' OR verification_status IS NULL",
            name="ck_artifact_verification_pdf_only",
        ),
        UniqueConstraint("canonical_paper_id", "artifact_key", name="uq_artifact_paper_key"),
        Index("ix_artifacts_paper_type", "canonical_paper_id", "artifact_type"),
        Index(
            "ix_artifacts_verified_pdf",
            "canonical_paper_id",
            postgresql_where=text(
                "artifact_type = 'SOURCE_PDF' AND status = 'ACTIVE' "
                "AND verification_status = 'VERIFIED'"
            ),
        ),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="CASCADE"), nullable=False
    )
    artifact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.blob_id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128))
    verification_status: Mapped[str | None] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class ItemArtifactOverride(TimestampMixin, Base):
    """The current user-selected content for one Library Item artifact key."""

    __tablename__ = "item_artifact_overrides"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "library_item_id", "canonical_paper_id"],
            [
                "library_items.library_id",
                "library_items.library_item_id",
                "library_items.canonical_paper_id",
            ],
            ondelete="CASCADE",
            name="fk_item_artifact_override_item_paper",
        ),
        Index("ix_item_artifact_overrides_blob", "blob_id"),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    library_item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    artifact_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.blob_id", ondelete="RESTRICT"), nullable=False
    )
    original_filename: Mapped[str | None] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    specified_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class Asset(TimestampMixin, Base):
    """An ordinary Library Item attachment, outside canonical Artifact selection."""

    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','DELETED')"),
        ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_asset_item_scope",
        ),
        Index("ix_assets_library_item", "library_id", "library_item_id", "created_at"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=False
    )
    library_item_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.blob_id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
