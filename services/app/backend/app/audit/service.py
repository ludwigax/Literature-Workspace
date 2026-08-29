from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEvent


def record_audit_event(
    session: AsyncSession,
    event_type: str,
    *,
    actor_principal_id: uuid.UUID | None = None,
    subject_principal_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        actor_principal_id=actor_principal_id,
        subject_principal_id=subject_principal_id,
        library_id=library_id,
        session_id=session_id,
        details=details or {},
    )
    session.add(event)
    return event
