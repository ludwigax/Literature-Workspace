"""Append-only application audit events."""

from .service import record_audit_event

__all__ = ["record_audit_event"]
