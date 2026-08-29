from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.authorization.dependencies import Actor
from backend.app.models import (
    CanonicalMetadata,
    CollectionItem,
    ItemTag,
    LibraryItem,
)
from backend.app.papers.service import paper_service


@dataclass(frozen=True, slots=True)
class LimitedItemResult:
    item: LibraryItem
    created: bool
    metadata_source: str


class LimitedItemService:
    """Initialize or reuse a Library Item from incomplete ingress metadata."""

    async def initialize(
        self,
        session: AsyncSession,
        *,
        actor: Actor,
        library_id: uuid.UUID,
        metadata: dict[str, Any],
        doi: str | None,
        identifiers: list[dict[str, str]] | None = None,
        collection_ids: list[uuid.UUID] | None = None,
        tag_ids: list[uuid.UUID] | None = None,
    ) -> LimitedItemResult:
        normalized_identifiers = paper_service.normalize_identifiers(
            identifiers
            if identifiers is not None
            else [{"scheme": "DOI", "value": doi}]
            if doi
            else []
        )
        for scheme, normalized, _ in sorted(normalized_identifiers):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"identifier:{scheme}:{normalized}"},
            )
        paper = await paper_service.resolve(session, normalized_identifiers)
        if paper is None:
            paper = await paper_service.create(
                session,
                actor,
                metadata=metadata,
                identifiers=normalized_identifiers,
            )
        item = await session.scalar(
            select(LibraryItem).where(
                LibraryItem.library_id == library_id,
                LibraryItem.canonical_paper_id == paper.canonical_paper_id,
                LibraryItem.status != "PURGED",
            )
        )
        created = item is None
        if item is None:
            item = LibraryItem(
                library_id=library_id,
                canonical_paper_id=paper.canonical_paper_id,
                item_type="PAPER",
                status="ACTIVE",
                local_overrides={},
                revision=1,
                saved_by=actor.principal_id,
            )
            session.add(item)
            await session.flush()

        await self._add_placements(
            session,
            item=item,
            actor_principal_id=actor.principal_id,
            collection_ids=collection_ids or [],
            tag_ids=tag_ids or [],
        )
        current = await session.get(CanonicalMetadata, paper.canonical_paper_id)
        if current is None:
            raise RuntimeError("Limited Item has no current canonical metadata")
        return LimitedItemResult(
            item=item,
            created=created,
            metadata_source=current.metadata_source,
        )

    @staticmethod
    async def _add_placements(
        session: AsyncSession,
        *,
        item: LibraryItem,
        actor_principal_id: uuid.UUID,
        collection_ids: list[uuid.UUID],
        tag_ids: list[uuid.UUID],
    ) -> None:
        existing_collections = set(
            await session.scalars(
                select(CollectionItem.collection_id).where(
                    CollectionItem.library_id == item.library_id,
                    CollectionItem.library_item_id == item.library_item_id,
                )
            )
        )
        for collection_id in set(collection_ids) - existing_collections:
            session.add(
                CollectionItem(
                    library_id=item.library_id,
                    collection_id=collection_id,
                    library_item_id=item.library_item_id,
                    added_by=actor_principal_id,
                )
            )
        existing_tags = set(
            await session.scalars(
                select(ItemTag.tag_id).where(
                    ItemTag.library_id == item.library_id,
                    ItemTag.library_item_id == item.library_item_id,
                )
            )
        )
        for tag_id in set(tag_ids) - existing_tags:
            session.add(
                ItemTag(
                    library_id=item.library_id,
                    tag_id=tag_id,
                    library_item_id=item.library_item_id,
                    added_by=actor_principal_id,
                )
            )
        await session.flush()


limited_item_service = LimitedItemService()
