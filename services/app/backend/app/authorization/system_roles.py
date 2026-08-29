from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import record_audit_event
from backend.app.models import ExternalIdentity, Principal, PrincipalSystemRole

from .dependencies import Actor


class SystemRoleService:
    async def list_principals(self, session: AsyncSession) -> list[dict[str, object]]:
        rows = (
            await session.execute(
                select(Principal, PrincipalSystemRole.role, ExternalIdentity.email)
                .outerjoin(
                    PrincipalSystemRole,
                    PrincipalSystemRole.principal_id == Principal.principal_id,
                )
                .outerjoin(
                    ExternalIdentity,
                    ExternalIdentity.principal_id == Principal.principal_id,
                )
                .order_by(Principal.display_name, Principal.principal_id)
            )
        ).all()
        return [
            {
                "principal_id": str(principal.principal_id),
                "display_name": principal.display_name,
                "status": principal.status,
                "email": email,
                "system_role": str(role or "USER"),
            }
            for principal, role, email in rows
        ]

    async def assign(
        self,
        session: AsyncSession,
        actor: Actor,
        principal_id: uuid.UUID,
        *,
        role: str,
    ) -> dict[str, object]:
        clean_role = role.strip().upper()
        if clean_role not in {"ADMIN", "USER"}:
            raise ValueError("system role must be ADMIN or USER")
        principal = await session.get(Principal, principal_id, with_for_update=True)
        if principal is None:
            raise LookupError("Principal not found")
        assignment = await session.get(PrincipalSystemRole, principal_id, with_for_update=True)
        previous = assignment.role if assignment is not None else "USER"
        if previous == "ADMIN" and clean_role == "USER":
            admin_count = int(
                await session.scalar(
                    select(func.count(PrincipalSystemRole.principal_id)).where(
                        PrincipalSystemRole.role == "ADMIN"
                    )
                )
                or 0
            )
            if admin_count <= 1:
                raise RuntimeError("The last ADMIN cannot be demoted")
        if assignment is None:
            assignment = PrincipalSystemRole(
                principal_id=principal_id,
                role=clean_role,
                assigned_by=actor.principal_id,
            )
            session.add(assignment)
        else:
            assignment.role = clean_role
            assignment.assigned_by = actor.principal_id
        record_audit_event(
            session,
            "system_role.changed",
            actor_principal_id=actor.principal_id,
            subject_principal_id=principal_id,
            session_id=actor.session_id,
            details={"previous": previous, "current": clean_role},
        )
        await session.flush()
        return {
            "principal_id": str(principal_id),
            "display_name": principal.display_name,
            "system_role": clean_role,
        }


system_role_service = SystemRoleService()
