from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class Principal(TimestampMixin, Base):
    __tablename__ = "principals"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','SUSPENDED','DELETED')"),)

    principal_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)

    identities: Mapped[list[ExternalIdentity]] = relationship(back_populates="principal")


class PrincipalSystemRole(TimestampMixin, Base):
    __tablename__ = "principal_system_roles"
    __table_args__ = (CheckConstraint("role IN ('ADMIN','USER')"),)

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), default="USER", nullable=False)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="SET NULL")
    )


class ExternalIdentity(TimestampMixin, Base):
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_external_identity"),)

    external_identity_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="CASCADE"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))

    principal: Mapped[Principal] = relationship(back_populates="identities")


class WebSession(TimestampMixin, Base):
    __tablename__ = "web_sessions"
    __table_args__ = (Index("ix_web_sessions_principal_expires", "principal_id", "expires_at"),)

    session_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oidc_refresh_token_ciphertext: Mapped[str | None] = mapped_column(String)


class OidcLoginAttempt(TimestampMixin, Base):
    __tablename__ = "oidc_login_attempts"
    __table_args__ = (Index("ix_oidc_attempts_expires", "expires_at"),)

    attempt_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier_ciphertext: Mapped[str] = mapped_column(String, nullable=False)
    return_path: Mapped[str] = mapped_column(String(500), default="/", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
