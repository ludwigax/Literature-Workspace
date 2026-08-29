from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record_audit_event
from ..authorization.dependencies import Actor, membership_for
from ..models import Collection, CollectionItem, LibraryItem


class CollectionService:
    async def list(
        self, session: AsyncSession, actor: Actor, library_id: uuid.UUID
    ) -> list[dict[str, object]]:
        await membership_for(session, actor=actor, library_id=library_id)
        rows = (
            await session.execute(
                select(
                    Collection,
                    func.count(CollectionItem.library_item_id).filter(
                        LibraryItem.status == "ACTIVE"
                    ),
                )
                .outerjoin(
                    CollectionItem,
                    (CollectionItem.library_id == Collection.library_id)
                    & (CollectionItem.collection_id == Collection.collection_id),
                )
                .outerjoin(
                    LibraryItem,
                    (LibraryItem.library_id == CollectionItem.library_id)
                    & (LibraryItem.library_item_id == CollectionItem.library_item_id),
                )
                .where(Collection.library_id == library_id, Collection.status == "ACTIVE")
                .group_by(Collection.collection_id)
                .order_by(Collection.name, Collection.collection_id)
            )
        ).all()
        return [self.view(collection, item_count) for collection, item_count in rows]

    async def create(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        name: str,
        parent_collection_id: uuid.UUID | None,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        clean_name = self.clean_name(name)
        if parent_collection_id is not None:
            await self.require_active(session, library_id, parent_collection_id)
        collection = Collection(
            library_id=library_id,
            parent_collection_id=parent_collection_id,
            name=clean_name,
            status="ACTIVE",
            revision=1,
            created_by=actor.principal_id,
        )
        session.add(collection)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Collection name already exists") from error
        record_audit_event(
            session,
            "library.collection_created",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"collection_id": str(collection.collection_id)},
        )
        await session.commit()
        return self.view(collection, 0)

    async def update(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        name: str,
        parent_collection_id: uuid.UUID | None,
        expected_revision: int,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        collection = await self.require_active(session, library_id, collection_id, lock=True)
        if collection.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Collection revision conflict")
        if parent_collection_id == collection_id:
            raise HTTPException(status_code=422, detail="Collection cannot contain itself")
        if parent_collection_id is not None:
            await self.require_active(session, library_id, parent_collection_id)
            if await self.is_descendant(
                session, library_id, ancestor_id=collection_id, candidate_id=parent_collection_id
            ):
                raise HTTPException(status_code=409, detail="Collection move would create a cycle")
        collection.name = self.clean_name(name)
        collection.parent_collection_id = parent_collection_id
        collection.revision += 1
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Collection name already exists") from error
        record_audit_event(
            session,
            "library.collection_updated",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"collection_id": str(collection_id)},
        )
        await session.flush()
        result = self.view(
            collection,
            await self.item_count(session, library_id, collection_id),
        )
        await session.commit()
        return result

    async def remove(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        expected_revision: int,
    ) -> None:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        collection = await self.require_active(session, library_id, collection_id, lock=True)
        if collection.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Collection revision conflict")
        child = await session.scalar(
            select(Collection.collection_id).where(
                Collection.library_id == library_id,
                Collection.parent_collection_id == collection_id,
                Collection.status == "ACTIVE",
            )
        )
        if child is not None:
            raise HTTPException(status_code=409, detail="Collection has active child Collections")
        await session.execute(
            delete(CollectionItem).where(
                CollectionItem.library_id == library_id,
                CollectionItem.collection_id == collection_id,
            )
        )
        collection.status = "DELETED"
        collection.revision += 1
        record_audit_event(
            session,
            "library.collection_deleted",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"collection_id": str(collection_id)},
        )
        await session.commit()

    async def add_item(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        collection_id: uuid.UUID,
        library_item_id: uuid.UUID,
    ) -> None:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        await self.require_active(session, library_id, collection_id)
        item = await session.scalar(
            select(LibraryItem).where(
                LibraryItem.library_id == library_id,
                LibraryItem.library_item_id == library_item_id,
                LibraryItem.status != "PURGED",
            )
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Library Item not found")
        existing = await session.get(
            CollectionItem,
            {
                "library_id": library_id,
                "collection_id": collection_id,
                "library_item_id": library_item_id,
            },
        )
        if existing is None:
            session.add(
                CollectionItem(
                    library_id=library_id,
                    collection_id=collection_id,
                    library_item_id=library_item_id,
                    added_by=actor.principal_id,
                )
            )
            await session.commit()

    async def remove_item(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        collection_id: uuid.UUID,
        library_item_id: uuid.UUID,
    ) -> None:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        await self.require_active(session, library_id, collection_id)
        await session.execute(
            delete(CollectionItem).where(
                CollectionItem.library_id == library_id,
                CollectionItem.collection_id == collection_id,
                CollectionItem.library_item_id == library_item_id,
            )
        )
        await session.commit()

    async def require_active(
        self,
        session: AsyncSession,
        library_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Collection:
        statement = select(Collection).where(
            Collection.library_id == library_id,
            Collection.collection_id == collection_id,
            Collection.status == "ACTIVE",
        )
        if lock:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return value

    async def is_descendant(
        self,
        session: AsyncSession,
        library_id: uuid.UUID,
        *,
        ancestor_id: uuid.UUID,
        candidate_id: uuid.UUID,
    ) -> bool:
        current: uuid.UUID | None = candidate_id
        visited: set[uuid.UUID] = set()
        while current is not None and current not in visited:
            if current == ancestor_id:
                return True
            visited.add(current)
            current = await session.scalar(
                select(Collection.parent_collection_id).where(
                    Collection.library_id == library_id,
                    Collection.collection_id == current,
                    Collection.status == "ACTIVE",
                )
            )
        return False

    @staticmethod
    async def item_count(
        session: AsyncSession, library_id: uuid.UUID, collection_id: uuid.UUID
    ) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CollectionItem)
                .join(
                    LibraryItem,
                    (LibraryItem.library_id == CollectionItem.library_id)
                    & (LibraryItem.library_item_id == CollectionItem.library_item_id),
                )
                .where(
                    CollectionItem.library_id == library_id,
                    CollectionItem.collection_id == collection_id,
                    LibraryItem.status == "ACTIVE",
                )
            )
            or 0
        )

    @staticmethod
    def clean_name(value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Collection name is required")
        return name

    @staticmethod
    def view(collection: Collection, item_count: int) -> dict[str, object]:
        return {
            "collection_id": str(collection.collection_id),
            "library_id": str(collection.library_id),
            "parent_collection_id": (
                str(collection.parent_collection_id) if collection.parent_collection_id else None
            ),
            "name": collection.name,
            "status": collection.status,
            "revision": collection.revision,
            "item_count": int(item_count),
        }


collection_service = CollectionService()
