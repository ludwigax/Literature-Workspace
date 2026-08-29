from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .catalogue import json_type


class ZoteroImportSource(TimestampMixin, Base):
    __tablename__ = "zotero_import_sources"
    __table_args__ = (
        UniqueConstraint("library_id", "source_identity", name="uq_zotero_source_identity"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=False
    )
    source_identity: Mapped[str] = mapped_column(String(300), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(300))
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ZoteroImportEntry(TimestampMixin, Base):
    __tablename__ = "zotero_import_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "library_item_id"],
            ["library_items.library_id", "library_items.library_item_id"],
            ondelete="CASCADE",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zotero_import_sources.source_id", ondelete="CASCADE"), primary_key=True
    )
    zotero_library_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    library_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    library_item_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    item_version: Mapped[int] = mapped_column(Integer, nullable=False)
    item_type: Mapped[str] = mapped_column(String(100), nullable=False)
    attachment_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        json_type, default=list, nullable=False
    )


class ZoteroCollectionMapping(TimestampMixin, Base):
    __tablename__ = "zotero_collection_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "collection_id"],
            ["collections.library_id", "collections.collection_id"],
            ondelete="CASCADE",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zotero_import_sources.source_id", ondelete="CASCADE"), primary_key=True
    )
    zotero_library_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    library_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    collection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
