from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditEvent(Base):
    """Append-only security and Library administration event."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_principal_time", "actor_principal_id", "occurred_at"),
        Index("ix_audit_events_library_time", "library_id", "occurred_at"),
        Index("ix_audit_events_type_time", "event_type", "occurred_at"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
    subject_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="SET NULL")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_sessions.session_id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=dict, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
