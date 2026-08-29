from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record_audit_event
from ..authorization.dependencies import Actor, membership_for
from ..models import ItemTag, LibraryItem, LibraryTag


class TagService:
    async def list(
        self, session: AsyncSession, actor: Actor, library_id: uuid.UUID
    ) -> list[dict[str, object]]:
        await membership_for(session, actor=actor, library_id=library_id)
        rows = (
            await session.execute(
                select(
                    LibraryTag,
                    func.count(ItemTag.library_item_id).filter(LibraryItem.status == "ACTIVE"),
                )
                .outerjoin(
                    ItemTag,
                    (ItemTag.library_id == LibraryTag.library_id)
                    & (ItemTag.tag_id == LibraryTag.tag_id),
                )
                .outerjoin(
                    LibraryItem,
                    (LibraryItem.library_id == ItemTag.library_id)
                    & (LibraryItem.library_item_id == ItemTag.library_item_id),
                )
                .where(LibraryTag.library_id == library_id, LibraryTag.status == "ACTIVE")
                .group_by(LibraryTag.tag_id)
                .order_by(LibraryTag.normalized_name, LibraryTag.tag_id)
            )
        ).all()
        return [self.view(tag, item_count) for tag, item_count in rows]

    async def create(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        name: str,
        color: str | None,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        clean_name, normalized_name = self.clean_name(name)
        tag = LibraryTag(
            library_id=library_id,
            name=clean_name,
            normalized_name=normalized_name,
            color=color,
            status="ACTIVE",
            revision=1,
            created_by=actor.principal_id,
        )
        session.add(tag)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Tag name already exists") from error
        record_audit_event(
            session,
            "library.tag_created",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"tag_id": str(tag.tag_id)},
        )
        await session.commit()
        return self.view(tag, 0)

    async def update(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        tag_id: uuid.UUID,
        *,
        name: str,
        color: str | None,
        expected_revision: int,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        tag = await self.require_active(session, library_id, tag_id, lock=True)
        if tag.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Tag revision conflict")
        tag.name, tag.normalized_name = self.clean_name(name)
        tag.color = color
        tag.revision += 1
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Tag name already exists") from error
        record_audit_event(
            session,
            "library.tag_updated",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"tag_id": str(tag_id)},
        )
        result = self.view(tag, await self.item_count(session, library_id, tag_id))
        await session.commit()
        return result

    async def remove(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        tag_id: uuid.UUID,
        *,
        expected_revision: int,
    ) -> None:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        tag = await self.require_active(session, library_id, tag_id, lock=True)
        if tag.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Tag revision conflict")
        await session.execute(
            delete(ItemTag).where(ItemTag.library_id == library_id, ItemTag.tag_id == tag_id)
        )
        tag.status = "DELETED"
        tag.revision += 1
        record_audit_event(
            session,
            "library.tag_deleted",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"tag_id": str(tag_id)},
        )
        await session.commit()

    async def require_active(
        self,
        session: AsyncSession,
        library_id: uuid.UUID,
        tag_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> LibraryTag:
        statement = select(LibraryTag).where(
            LibraryTag.library_id == library_id,
            LibraryTag.tag_id == tag_id,
            LibraryTag.status == "ACTIVE",
        )
        if lock:
            statement = statement.with_for_update()
        tag = await session.scalar(statement)
        if tag is None:
            raise HTTPException(status_code=404, detail="Tag not found")
        return tag

    @staticmethod
    async def item_count(session: AsyncSession, library_id: uuid.UUID, tag_id: uuid.UUID) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ItemTag)
                .join(
                    LibraryItem,
                    (LibraryItem.library_id == ItemTag.library_id)
                    & (LibraryItem.library_item_id == ItemTag.library_item_id),
                )
                .where(
                    ItemTag.library_id == library_id,
                    ItemTag.tag_id == tag_id,
                    LibraryItem.status == "ACTIVE",
                )
            )
            or 0
        )

    @staticmethod
    def clean_name(value: str) -> tuple[str, str]:
        name = " ".join(str(value or "").split())
        if not name:
            raise HTTPException(status_code=422, detail="Tag name is required")
        return name, name.casefold()

    @staticmethod
    def view(tag: LibraryTag, item_count: int) -> dict[str, object]:
        return {
            "tag_id": str(tag.tag_id),
            "library_id": str(tag.library_id),
            "name": tag.name,
            "color": tag.color,
            "status": tag.status,
            "revision": tag.revision,
            "item_count": int(item_count),
        }


tag_service = TagService()
