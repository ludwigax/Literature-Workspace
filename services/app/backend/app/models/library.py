from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Library(TimestampMixin, Base):
    __tablename__ = "libraries"
    __table_args__ = (
        CheckConstraint("library_type IN ('PERSONAL','GROUP')"),
        CheckConstraint("status IN ('ACTIVE','ARCHIVED','DELETED')"),
        Index(
            "uq_personal_library_owner",
            "owner_principal_id",
            unique=True,
            postgresql_where=text("library_type = 'PERSONAL' AND status <> 'DELETED'"),
        ),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class LibraryMembership(TimestampMixin, Base):
    __tablename__ = "library_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('OWNER','EDITOR','VIEWER')"),
        CheckConstraint("status IN ('ACTIVE','INVITED','REVOKED')"),
        Index("ix_memberships_principal_status", "principal_id", "status"),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), primary_key=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)


class LibraryInvitation(TimestampMixin, Base):
    __tablename__ = "library_invitations"
    __table_args__ = (
        CheckConstraint("role IN ('EDITOR','VIEWER')"),
        CheckConstraint("status IN ('PENDING','ACCEPTED','REVOKED','EXPIRED')"),
        Index("ix_library_invitations_lookup", "library_id", "email_normalized", "status"),
        Index("ix_library_invitations_expires", "expires_at"),
    )

    invitation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=False
    )
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT"), nullable=False
    )
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
