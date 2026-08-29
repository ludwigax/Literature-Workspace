from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

json_type = JSON().with_variant(JSONB, "postgresql")


class CanonicalPaper(TimestampMixin, Base):
    __tablename__ = "canonical_papers"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','MERGED','DELETED')"),)

    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)


class CanonicalIdentifier(TimestampMixin, Base):
    __tablename__ = "canonical_identifiers"
    __table_args__ = (
        CheckConstraint("scheme IN ('DOI','PMID','ARXIV','ISBN','OTHER')"),
        UniqueConstraint("scheme", "normalized_value", name="uq_canonical_identifier"),
    )

    identifier_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="CASCADE"), nullable=False
    )
    scheme: Mapped[str] = mapped_column(String(20), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(500), nullable=False)
    original_value: Mapped[str] = mapped_column(String(500), nullable=False)


class CanonicalMetadata(TimestampMixin, Base):
    __tablename__ = "canonical_metadata"
    __table_args__ = (
        CheckConstraint("metadata_source IN ('UNDEFINED','CROSSREF','OPENALEX','ARXIV','ZOTERO')"),
        CheckConstraint("publication_year IS NULL OR publication_year BETWEEN 1000 AND 3000"),
        CheckConstraint("publication_month IS NULL OR publication_month BETWEEN 1 AND 12"),
        CheckConstraint("publication_day IS NULL OR publication_day BETWEEN 1 AND 31"),
        CheckConstraint(
            "publication_date_precision IS NULL OR "
            "publication_date_precision IN ('YEAR','MONTH','DAY')"
        ),
    )

    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="CASCADE"), primary_key=True
    )
    metadata_source: Mapped[str] = mapped_column(String(20), default="UNDEFINED", nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    publication_month: Mapped[int | None] = mapped_column(SmallInteger)
    publication_day: Mapped[int | None] = mapped_column(SmallInteger)
    publication_date: Mapped[date | None] = mapped_column(Date)
    publication_date_precision: Mapped[str | None] = mapped_column(String(10))
    work_type: Mapped[str | None] = mapped_column(String(50))
    venue: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    volume: Mapped[str | None] = mapped_column(String(200))
    issue: Mapped[str | None] = mapped_column(String(200))
    pages: Mapped[str | None] = mapped_column(String(200))
    article_number: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(100))
    issn: Mapped[list[str]] = mapped_column(json_type, default=list, nullable=False)
    isbn: Mapped[list[str]] = mapped_column(json_type, default=list, nullable=False)
    authors: Mapped[list[dict[str, Any]]] = mapped_column(json_type, default=list, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class LibraryItem(TimestampMixin, Base):
    __tablename__ = "library_items"
    __table_args__ = (
        CheckConstraint("item_type IN ('PAPER')"),
        CheckConstraint("status IN ('ACTIVE','TRASHED','PURGED')"),
        UniqueConstraint("library_id", "library_item_id", name="uq_library_item_scope"),
        UniqueConstraint(
            "library_id",
            "library_item_id",
            "canonical_paper_id",
            name="uq_library_item_paper_scope",
        ),
        Index(
            "uq_active_library_canonical_paper",
            "library_id",
            "canonical_paper_id",
            unique=True,
            postgresql_where=text("status <> 'PURGED'"),
        ),
        Index("ix_library_items_library_status_created", "library_id", "status", "created_at"),
    )

    library_item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=False
    )
    canonical_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_papers.canonical_paper_id", ondelete="RESTRICT"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(String(20), default="PAPER", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    local_overrides: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    saved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trashed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class Collection(TimestampMixin, Base):
    __tablename__ = "collections"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','DELETED')"),
        CheckConstraint("parent_collection_id IS NULL OR parent_collection_id <> collection_id"),
        UniqueConstraint("library_id", "collection_id", name="uq_collection_scope"),
        ForeignKeyConstraint(
            ["library_id", "parent_collection_id"],
            ["collections.library_id", "collections.collection_id"],
            ondelete="RESTRICT",
            name="fk_collection_parent_scope",
        ),
        Index("ix_collections_library_parent", "library_id", "parent_collection_id", "name"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=False
    )
    parent_collection_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class CollectionItem(Base):
    __tablename__ = "collection_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "collection_id"],
            ["collections.library_id", "collections.collection_id"],
            ondelete="CASCADE",
            name="fk_collection_item_collection_scope",
        ),
        ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_collection_item_item_scope",
        ),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    library_item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LibraryTag(TimestampMixin, Base):
    __tablename__ = "library_tags"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','DELETED')"),
        UniqueConstraint("library_id", "tag_id", name="uq_library_tag_scope"),
        Index(
            "uq_active_library_tag_name",
            "library_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    tag_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class ItemTag(Base):
    __tablename__ = "item_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "tag_id"],
            ["library_tags.library_id", "library_tags.tag_id"],
            ondelete="CASCADE",
            name="fk_item_tag_tag_scope",
        ),
        ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
            name="fk_item_tag_item_scope",
        ),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    library_item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
