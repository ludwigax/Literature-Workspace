from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record_audit_event
from ..authorization.dependencies import Actor, membership_for
from ..identity.security import hash_token, random_token
from ..models import (
    ExternalIdentity,
    Library,
    LibraryInvitation,
    LibraryMembership,
    Principal,
)


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: LibraryInvitation
    accept_token: str


class LibraryService:
    async def list_for_actor(self, session: AsyncSession, actor: Actor) -> list[dict[str, object]]:
        rows = (
            await session.execute(
                select(Library, LibraryMembership)
                .join(
                    LibraryMembership,
                    LibraryMembership.library_id == Library.library_id,
                )
                .where(
                    LibraryMembership.principal_id == actor.principal_id,
                    LibraryMembership.status == "ACTIVE",
                    Library.status != "DELETED",
                )
                .order_by(Library.library_type, Library.name, Library.library_id)
            )
        ).all()
        return [self.library_view(library, membership.role) for library, membership in rows]

    async def get_for_actor(
        self, session: AsyncSession, actor: Actor, library_id: uuid.UUID
    ) -> dict[str, object]:
        membership = await membership_for(session, actor=actor, library_id=library_id)
        library = await session.get(Library, library_id)
        if library is None or library.status == "DELETED":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="library not found")
        return self.library_view(library, membership.role)

    async def create_group(
        self, session: AsyncSession, actor: Actor, *, name: str
    ) -> dict[str, object]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise HTTPException(status_code=422, detail="Library name is required")
        library = Library(
            library_type="GROUP",
            name=clean_name,
            owner_principal_id=actor.principal_id,
            status="ACTIVE",
            revision=1,
        )
        session.add(library)
        await session.flush()
        membership = LibraryMembership(
            library_id=library.library_id,
            principal_id=actor.principal_id,
            role="OWNER",
            status="ACTIVE",
        )
        session.add(membership)
        record_audit_event(
            session,
            "library.group_created",
            actor_principal_id=actor.principal_id,
            subject_principal_id=actor.principal_id,
            library_id=library.library_id,
        )
        await session.commit()
        return self.library_view(library, membership.role)

    async def list_members(
        self, session: AsyncSession, actor: Actor, library_id: uuid.UUID
    ) -> list[dict[str, object]]:
        await membership_for(session, actor=actor, library_id=library_id)
        rows = (
            await session.execute(
                select(LibraryMembership, Principal)
                .join(Principal, Principal.principal_id == LibraryMembership.principal_id)
                .where(
                    LibraryMembership.library_id == library_id,
                    LibraryMembership.status == "ACTIVE",
                )
                .order_by(Principal.display_name, Principal.principal_id)
            )
        ).all()
        return [
            {
                "principal_id": str(principal.principal_id),
                "display_name": principal.display_name,
                "role": membership.role,
                "status": membership.status,
            }
            for membership, principal in rows
        ]

    async def create_invitation(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        email: str,
        role: str,
    ) -> CreatedInvitation:
        await membership_for(
            session,
            actor=actor,
            library_id=library_id,
            allowed_roles={"OWNER"},
        )
        library = await session.get(Library, library_id)
        if library is None or library.library_type != "GROUP" or library.status != "ACTIVE":
            raise HTTPException(status_code=404, detail="group Library not found")
        normalized_email = self.normalize_email(email)
        normalized_role = str(role or "").upper()
        if normalized_role not in {"EDITOR", "VIEWER"}:
            raise HTTPException(status_code=422, detail="invitation role must be EDITOR or VIEWER")
        existing = await session.scalar(
            select(LibraryInvitation).where(
                LibraryInvitation.library_id == library_id,
                LibraryInvitation.email_normalized == normalized_email,
                LibraryInvitation.status == "PENDING",
                LibraryInvitation.expires_at > datetime.now(UTC),
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="an active invitation already exists")
        token = random_token(48)
        invitation = LibraryInvitation(
            library_id=library_id,
            email_normalized=normalized_email,
            role=normalized_role,
            token_hash=hash_token(token),
            status="PENDING",
            invited_by=actor.principal_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        session.add(invitation)
        library.revision += 1
        await session.flush()
        record_audit_event(
            session,
            "library.invitation_created",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={
                "invitation_id": str(invitation.invitation_id),
                "role": invitation.role,
            },
        )
        await session.commit()
        return CreatedInvitation(invitation=invitation, accept_token=token)

    async def list_invitations(
        self, session: AsyncSession, actor: Actor, library_id: uuid.UUID
    ) -> list[dict[str, object]]:
        await membership_for(
            session,
            actor=actor,
            library_id=library_id,
            allowed_roles={"OWNER"},
        )
        values = (
            await session.scalars(
                select(LibraryInvitation)
                .where(LibraryInvitation.library_id == library_id)
                .order_by(LibraryInvitation.created_at.desc())
            )
        ).all()
        return [self.invitation_view(value) for value in values]

    async def regenerate_invitation(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        invitation_id: uuid.UUID,
    ) -> CreatedInvitation:
        await membership_for(
            session,
            actor=actor,
            library_id=library_id,
            allowed_roles={"OWNER"},
        )
        invitation = await session.scalar(
            select(LibraryInvitation)
            .where(
                LibraryInvitation.invitation_id == invitation_id,
                LibraryInvitation.library_id == library_id,
            )
            .with_for_update()
        )
        if invitation is None:
            raise HTTPException(status_code=404, detail="invitation not found")
        if invitation.status == "ACCEPTED":
            raise HTTPException(status_code=409, detail="accepted invitation cannot be regenerated")

        previous_status = invitation.status
        token = random_token(48)
        invitation.token_hash = hash_token(token)
        invitation.status = "PENDING"
        invitation.invited_by = actor.principal_id
        invitation.accepted_by = None
        invitation.accepted_at = None
        invitation.expires_at = datetime.now(UTC) + timedelta(days=7)
        await self._bump_revision(session, library_id)
        record_audit_event(
            session,
            "library.invitation_regenerated",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={
                "invitation_id": str(invitation.invitation_id),
                "previous_status": previous_status,
                "role": invitation.role,
            },
        )
        await session.commit()
        return CreatedInvitation(invitation=invitation, accept_token=token)

    async def accept_invitation(
        self, session: AsyncSession, actor: Actor, *, token: str
    ) -> dict[str, object]:
        invitation = await session.scalar(
            select(LibraryInvitation)
            .where(LibraryInvitation.token_hash == hash_token(token))
            .with_for_update()
        )
        now = datetime.now(UTC)
        if invitation is None or invitation.status != "PENDING":
            raise HTTPException(status_code=404, detail="invitation not found")
        if invitation.expires_at <= now:
            invitation.status = "EXPIRED"
            await session.commit()
            raise HTTPException(status_code=410, detail="invitation expired")
        emails = set(
            await session.scalars(
                select(ExternalIdentity.email).where(
                    ExternalIdentity.principal_id == actor.principal_id,
                    ExternalIdentity.email.is_not(None),
                )
            )
        )
        normalized_emails = {self.normalize_email(email) for email in emails if email}
        if invitation.email_normalized not in normalized_emails:
            raise HTTPException(status_code=403, detail="invitation belongs to another identity")
        existing_membership = await session.get(
            LibraryMembership,
            {"library_id": invitation.library_id, "principal_id": actor.principal_id},
        )
        if existing_membership is None:
            membership = LibraryMembership(
                library_id=invitation.library_id,
                principal_id=actor.principal_id,
                role=invitation.role,
                status="ACTIVE",
            )
            new_membership = True
        else:
            membership = existing_membership
            new_membership = False
        invitation.status = "ACCEPTED"
        invitation.accepted_by = actor.principal_id
        invitation.accepted_at = now
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.invitation_id', :invitation_id, true)"),
            {"invitation_id": str(invitation.invitation_id)},
        )
        if new_membership:
            session.add(membership)
        elif membership.role != "OWNER":
            membership.role = invitation.role
            membership.status = "ACTIVE"
        await session.flush()
        library = await session.get(Library, invitation.library_id)
        if library is None or library.status != "ACTIVE":
            raise HTTPException(status_code=404, detail="library not found")
        library.revision += 1
        record_audit_event(
            session,
            "library.invitation_accepted",
            actor_principal_id=actor.principal_id,
            subject_principal_id=actor.principal_id,
            library_id=invitation.library_id,
            details={
                "invitation_id": str(invitation.invitation_id),
                "role": membership.role,
            },
        )
        await session.flush()
        await session.refresh(library)
        result = self.library_view(library, membership.role)
        await session.commit()
        return result

    async def update_member_role(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        principal_id: uuid.UUID,
        *,
        role: str,
    ) -> dict[str, object]:
        await membership_for(
            session,
            actor=actor,
            library_id=library_id,
            allowed_roles={"OWNER"},
        )
        normalized_role = str(role or "").upper()
        if normalized_role not in {"OWNER", "EDITOR", "VIEWER"}:
            raise HTTPException(status_code=422, detail="invalid role")
        target = await session.get(
            LibraryMembership,
            {"library_id": library_id, "principal_id": principal_id},
        )
        if target is None or target.status != "ACTIVE":
            raise HTTPException(status_code=404, detail="member not found")
        if target.role == "OWNER" and normalized_role != "OWNER":
            await self._ensure_other_owner(session, library_id, principal_id)
        previous_role = target.role
        target.role = normalized_role
        await self._bump_revision(session, library_id)
        record_audit_event(
            session,
            "library.membership_role_changed",
            actor_principal_id=actor.principal_id,
            subject_principal_id=principal_id,
            library_id=library_id,
            details={"from_role": previous_role, "to_role": normalized_role},
        )
        await session.commit()
        return {
            "principal_id": str(target.principal_id),
            "role": target.role,
            "status": target.status,
        }

    async def remove_member(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        await membership_for(
            session,
            actor=actor,
            library_id=library_id,
            allowed_roles={"OWNER"},
        )
        target = await session.get(
            LibraryMembership,
            {"library_id": library_id, "principal_id": principal_id},
        )
        if target is None or target.status != "ACTIVE":
            raise HTTPException(status_code=404, detail="member not found")
        if target.role == "OWNER":
            await self._ensure_other_owner(session, library_id, principal_id)
        target.status = "REVOKED"
        await self._bump_revision(session, library_id)
        record_audit_event(
            session,
            "library.membership_revoked",
            actor_principal_id=actor.principal_id,
            subject_principal_id=principal_id,
            library_id=library_id,
            details={"role": target.role},
        )
        await session.commit()

    async def _ensure_other_owner(
        self, session: AsyncSession, library_id: uuid.UUID, principal_id: uuid.UUID
    ) -> None:
        other_owners = await session.scalar(
            select(func.count())
            .select_from(LibraryMembership)
            .where(
                LibraryMembership.library_id == library_id,
                LibraryMembership.principal_id != principal_id,
                LibraryMembership.role == "OWNER",
                LibraryMembership.status == "ACTIVE",
            )
        )
        if not other_owners:
            raise HTTPException(status_code=409, detail="a group Library must retain an owner")

    @staticmethod
    async def _bump_revision(session: AsyncSession, library_id: uuid.UUID) -> None:
        library = await session.get(Library, library_id)
        if library is not None:
            library.revision += 1

    @staticmethod
    def library_view(library: Library, role: str) -> dict[str, object]:
        return {
            "library_id": str(library.library_id),
            "library_type": library.library_type,
            "name": library.name,
            "status": library.status,
            "role": role,
            "revision": library.revision,
            "updated_at": library.updated_at.isoformat(),
        }

    @staticmethod
    def invitation_view(invitation: LibraryInvitation) -> dict[str, object]:
        return {
            "invitation_id": str(invitation.invitation_id),
            "library_id": str(invitation.library_id),
            "email": invitation.email_normalized,
            "role": invitation.role,
            "status": invitation.status,
            "expires_at": invitation.expires_at.isoformat(),
        }

    @staticmethod
    def normalize_email(value: str) -> str:
        email = str(value or "").strip().casefold()
        if not email or "@" not in email or len(email) > 320:
            raise HTTPException(status_code=422, detail="valid email is required")
        return email


library_service = LibraryService()
