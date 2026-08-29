from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Text, and_, case, cast, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record_audit_event
from ..authorization.dependencies import Actor, membership_for
from ..collections.service import collection_service
from ..models import (
    Artifact,
    Asset,
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    Collection,
    CollectionItem,
    ItemArtifactOverride,
    ItemTag,
    LibraryItem,
)
from ..papers.service import paper_service
from ..tags.service import tag_service

EDITABLE_METADATA_KEYS = {
    "title",
    "abstract",
    "publication_year",
    "publication_month",
    "publication_day",
    "publication_date",
    "publication_date_precision",
    "work_type",
    "venue",
    "canonical_url",
    "publisher",
    "volume",
    "issue",
    "pages",
    "article_number",
    "language",
    "issn",
    "isbn",
    "authors",
    "extra",
}


class LibraryItemService:
    async def create_item(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        metadata: dict[str, Any],
        identifiers: list[dict[str, str]],
        collection_ids: list[uuid.UUID],
        tag_ids: list[uuid.UUID],
        local_overrides: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        title = str(metadata.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=422, detail="title is required")
        normalized_identifiers = paper_service.normalize_identifiers(identifiers)
        if normalized_identifiers:
            lock_key = "\x1f".join(
                f"{scheme}:{value}" for scheme, value, _ in normalized_identifiers
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
        paper = await paper_service.resolve(session, normalized_identifiers)
        if paper is None:
            paper = await paper_service.create(
                session,
                actor,
                metadata=metadata,
                identifiers=normalized_identifiers,
            )
        existing = await session.scalar(
            select(LibraryItem).where(
                LibraryItem.library_id == library_id,
                LibraryItem.canonical_paper_id == paper.canonical_paper_id,
                LibraryItem.status != "PURGED",
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Paper is already saved in this Library")
        unique_collection_ids = list(dict.fromkeys(collection_ids))
        for collection_id in unique_collection_ids:
            await collection_service.require_active(session, library_id, collection_id)
        unique_tag_ids = list(dict.fromkeys(tag_ids))
        for tag_id in unique_tag_ids:
            await tag_service.require_active(session, library_id, tag_id)
        overrides = self.normalize_overrides(local_overrides or {}, allow_removal=False)
        item = LibraryItem(
            library_id=library_id,
            canonical_paper_id=paper.canonical_paper_id,
            item_type="PAPER",
            status="ACTIVE",
            local_overrides=overrides,
            revision=1,
            saved_by=actor.principal_id,
        )
        session.add(item)
        await session.flush()
        for collection_id in unique_collection_ids:
            session.add(
                CollectionItem(
                    library_id=library_id,
                    collection_id=collection_id,
                    library_item_id=item.library_item_id,
                    added_by=actor.principal_id,
                )
            )
        for tag_id in unique_tag_ids:
            session.add(
                ItemTag(
                    library_id=library_id,
                    tag_id=tag_id,
                    library_item_id=item.library_item_id,
                    added_by=actor.principal_id,
                )
            )
        record_audit_event(
            session,
            "library.item_created",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={
                "library_item_id": str(item.library_item_id),
                "canonical_paper_id": str(paper.canonical_paper_id),
            },
        )
        result = await self.view(
            session, item, collection_ids=unique_collection_ids, tag_ids=unique_tag_ids
        )
        await session.commit()
        return result

    async def list_items(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        status: str,
        collection_id: uuid.UUID | None,
        tag_id: uuid.UUID | None,
        query: str | None,
        title: str | None,
        author: str | None,
        identifier: str | None,
        venue: str | None,
        year_from: int | None,
        year_to: int | None,
        work_types: Sequence[str],
        metadata_sources: Sequence[str],
        collection_ids: Sequence[uuid.UUID],
        tag_ids: Sequence[uuid.UUID],
        tag_mode: str,
        include_subcollections: bool,
        has_pdf: bool | None,
        has_document: bool | None,
        has_asset: bool | None,
        added_from: date | None,
        added_to: date | None,
        modified_from: date | None,
        modified_to: date | None,
        sort: str,
        direction: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, object]], str | None]:
        await membership_for(session, actor=actor, library_id=library_id)
        normalized_status = status.upper()
        if normalized_status not in {"ACTIVE", "TRASHED"}:
            raise HTTPException(status_code=422, detail="status must be ACTIVE or TRASHED")
        normalized_tag_mode = tag_mode.upper()
        normalized_sort = sort.upper()
        normalized_direction = direction.upper()
        if normalized_tag_mode not in {"ANY", "ALL"}:
            raise HTTPException(status_code=422, detail="tag_mode must be ANY or ALL")
        if normalized_sort not in {"ADDED", "MODIFIED", "TITLE", "AUTHOR", "YEAR"}:
            raise HTTPException(status_code=422, detail="invalid catalogue sort")
        if normalized_direction not in {"ASC", "DESC"}:
            raise HTTPException(status_code=422, detail="direction must be ASC or DESC")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise HTTPException(status_code=422, detail="year_from must not exceed year_to")
        for start, end, label in (
            (added_from, added_to, "added"),
            (modified_from, modified_to, "modified"),
        ):
            if start is not None and end is not None and start > end:
                raise HTTPException(
                    status_code=422, detail=f"{label}_from must not exceed {label}_to"
                )

        statement = select(LibraryItem).where(
            LibraryItem.library_id == library_id,
            LibraryItem.status == normalized_status,
        )
        search_terms = self.metadata_search_terms(query)
        field_terms = {
            "title": self.metadata_search_value(title),
            "author": self.metadata_search_value(author),
            "identifier": self.metadata_search_value(identifier),
            "venue": self.metadata_search_value(venue),
        }
        needs_metadata = bool(
            search_terms
            or field_terms["title"]
            or field_terms["author"]
            or field_terms["venue"]
            or year_from is not None
            or year_to is not None
            or work_types
            or metadata_sources
            or normalized_sort in {"TITLE", "AUTHOR", "YEAR"}
        )
        if needs_metadata:
            statement = statement.join(
                CanonicalMetadata,
                CanonicalMetadata.canonical_paper_id == LibraryItem.canonical_paper_id,
            )
        if search_terms:
            canonical_fields = (
                CanonicalMetadata.title,
                cast(CanonicalMetadata.authors, Text),
                CanonicalMetadata.venue,
                CanonicalMetadata.publisher,
                cast(CanonicalMetadata.publication_year, Text),
                CanonicalMetadata.work_type,
                CanonicalMetadata.canonical_url,
                CanonicalMetadata.language,
                cast(CanonicalMetadata.issn, Text),
                cast(CanonicalMetadata.isbn, Text),
            )
            local_metadata = cast(LibraryItem.local_overrides, Text)
            for term in search_terms:
                identifier_match = (
                    select(CanonicalIdentifier.identifier_id)
                    .where(
                        CanonicalIdentifier.canonical_paper_id
                        == LibraryItem.canonical_paper_id,
                        or_(
                            func.lower(CanonicalIdentifier.scheme).contains(
                                term, autoescape=True
                            ),
                            func.lower(CanonicalIdentifier.normalized_value).contains(
                                term, autoescape=True
                            ),
                            func.lower(CanonicalIdentifier.original_value).contains(
                                term, autoescape=True
                            ),
                        ),
                    )
                    .exists()
                )
                statement = statement.where(
                    or_(
                        *[
                            func.lower(field).contains(term, autoescape=True)
                            for field in canonical_fields
                        ],
                        func.lower(local_metadata).contains(term, autoescape=True),
                        identifier_match,
                    )
                )

        effective_title = func.coalesce(
            LibraryItem.local_overrides["title"].as_string(), CanonicalMetadata.title
        )
        effective_authors = func.coalesce(
            LibraryItem.local_overrides["authors"].as_string(),
            cast(CanonicalMetadata.authors, Text),
        )
        effective_venue = func.coalesce(
            LibraryItem.local_overrides["venue"].as_string(), CanonicalMetadata.venue
        )
        effective_year = func.coalesce(
            LibraryItem.local_overrides["publication_year"].as_integer(),
            CanonicalMetadata.publication_year,
        )
        for field, expression in (
            (field_terms["title"], effective_title),
            (field_terms["author"], effective_authors),
            (field_terms["venue"], effective_venue),
        ):
            if field:
                statement = statement.where(
                    func.lower(expression).contains(field, autoescape=True)
                )
        if field_terms["identifier"]:
            identifier_term = field_terms["identifier"]
            statement = statement.where(
                select(CanonicalIdentifier.identifier_id)
                .where(
                    CanonicalIdentifier.canonical_paper_id == LibraryItem.canonical_paper_id,
                    or_(
                        func.lower(CanonicalIdentifier.scheme).contains(
                            identifier_term, autoescape=True
                        ),
                        func.lower(CanonicalIdentifier.normalized_value).contains(
                            identifier_term, autoescape=True
                        ),
                        func.lower(CanonicalIdentifier.original_value).contains(
                            identifier_term, autoescape=True
                        ),
                    ),
                )
                .exists()
            )
        if year_from is not None:
            statement = statement.where(effective_year >= year_from)
        if year_to is not None:
            statement = statement.where(effective_year <= year_to)
        normalized_work_types = [value.strip().casefold() for value in work_types if value.strip()]
        if normalized_work_types:
            effective_work_type = func.lower(
                func.coalesce(
                    LibraryItem.local_overrides["work_type"].as_string(),
                    CanonicalMetadata.work_type,
                )
            )
            statement = statement.where(effective_work_type.in_(normalized_work_types))
        normalized_sources = [value.strip().upper() for value in metadata_sources if value.strip()]
        if normalized_sources:
            statement = statement.where(CanonicalMetadata.metadata_source.in_(normalized_sources))

        selected_collections = list(dict.fromkeys(
            ([collection_id] if collection_id is not None else []) + list(collection_ids)
        ))
        if selected_collections:
            for selected_collection_id in selected_collections:
                await collection_service.require_active(
                    session, library_id, selected_collection_id
                )
            if include_subcollections:
                selected_collections = await self.expand_collection_ids(
                    session, library_id, selected_collections
                )
            statement = statement.where(
                select(CollectionItem.library_item_id)
                .where(
                    CollectionItem.library_id == LibraryItem.library_id,
                    CollectionItem.library_item_id == LibraryItem.library_item_id,
                    CollectionItem.collection_id.in_(selected_collections),
                )
                .exists()
            )
        selected_tags = list(
            dict.fromkeys(([tag_id] if tag_id is not None else []) + list(tag_ids))
        )
        if selected_tags:
            for selected_tag_id in selected_tags:
                await tag_service.require_active(session, library_id, selected_tag_id)
            if normalized_tag_mode == "ANY":
                statement = statement.where(
                    select(ItemTag.library_item_id)
                    .where(
                        ItemTag.library_id == LibraryItem.library_id,
                        ItemTag.library_item_id == LibraryItem.library_item_id,
                        ItemTag.tag_id.in_(selected_tags),
                    )
                    .exists()
                )
            else:
                for selected_tag_id in selected_tags:
                    statement = statement.where(
                        select(ItemTag.library_item_id)
                        .where(
                            ItemTag.library_id == LibraryItem.library_id,
                            ItemTag.library_item_id == LibraryItem.library_item_id,
                            ItemTag.tag_id == selected_tag_id,
                        )
                        .exists()
                    )

        pdf_exists = or_(
            select(ItemArtifactOverride.library_item_id)
            .where(
                ItemArtifactOverride.library_id == LibraryItem.library_id,
                ItemArtifactOverride.library_item_id == LibraryItem.library_item_id,
                ItemArtifactOverride.artifact_type == "SOURCE_PDF",
            )
            .exists(),
            select(Artifact.artifact_id)
            .where(
                Artifact.canonical_paper_id == LibraryItem.canonical_paper_id,
                Artifact.artifact_type == "SOURCE_PDF",
                Artifact.status == "ACTIVE",
            )
            .exists(),
        )
        document_exists = (
            select(Artifact.artifact_id)
            .where(
                Artifact.canonical_paper_id == LibraryItem.canonical_paper_id,
                Artifact.artifact_type == "PIPELINE_DOCUMENT",
                Artifact.status == "ACTIVE",
            )
            .exists()
        )
        asset_exists = (
            select(Asset.asset_id)
            .where(
                Asset.library_id == LibraryItem.library_id,
                Asset.library_item_id == LibraryItem.library_item_id,
                Asset.status == "ACTIVE",
            )
            .exists()
        )
        for expected, resource_expression in (
            (has_pdf, pdf_exists),
            (has_document, document_exists),
            (has_asset, asset_exists),
        ):
            if expected is not None:
                statement = statement.where(
                    resource_expression if expected else ~resource_expression
                )

        for value, date_expression, inclusive_end in (
            (added_from, LibraryItem.created_at, False),
            (added_to, LibraryItem.created_at, True),
            (modified_from, LibraryItem.updated_at, False),
            (modified_to, LibraryItem.updated_at, True),
        ):
            if value is not None:
                boundary = datetime.combine(value, time.min, tzinfo=UTC)
                statement = statement.where(
                    date_expression < boundary + timedelta(days=1)
                    if inclusive_end
                    else date_expression >= boundary
                )

        raw_sort_key: Any
        safe_sort_key: Any
        if normalized_sort == "ADDED":
            raw_sort_key = LibraryItem.created_at
            safe_sort_key = raw_sort_key
        elif normalized_sort == "MODIFIED":
            raw_sort_key = LibraryItem.updated_at
            safe_sort_key = raw_sort_key
        elif normalized_sort == "TITLE":
            raw_sort_key = func.lower(effective_title)
            safe_sort_key = func.coalesce(raw_sort_key, "")
        elif normalized_sort == "AUTHOR":
            local_first_author = func.coalesce(
                LibraryItem.local_overrides["authors"][0]["family"].as_string(),
                LibraryItem.local_overrides["authors"][0]["name"].as_string(),
                LibraryItem.local_overrides["authors"][0]["given"].as_string(),
            )
            canonical_first_author = func.coalesce(
                CanonicalMetadata.authors[0]["family"].as_string(),
                CanonicalMetadata.authors[0]["name"].as_string(),
                CanonicalMetadata.authors[0]["given"].as_string(),
            )
            raw_sort_key = func.lower(
                func.coalesce(local_first_author, canonical_first_author)
            )
            safe_sort_key = func.coalesce(raw_sort_key, "")
        else:
            raw_sort_key = effective_year
            safe_sort_key = func.coalesce(raw_sort_key, 0)
        null_rank = case((raw_sort_key.is_(None), 1), else_=0)
        if cursor is not None:
            cursor_null, cursor_value, cursor_item_id = self.decode_cursor(
                cursor, sort=normalized_sort, direction=normalized_direction
            )
            key_comparison = (
                safe_sort_key > cursor_value
                if normalized_direction == "ASC"
                else safe_sort_key < cursor_value
            )
            id_comparison = (
                LibraryItem.library_item_id > cursor_item_id
                if normalized_direction == "ASC"
                else LibraryItem.library_item_id < cursor_item_id
            )
            statement = statement.where(
                or_(
                    null_rank > cursor_null,
                    and_(
                        null_rank == cursor_null,
                        or_(
                            key_comparison,
                            and_(safe_sort_key == cursor_value, id_comparison),
                        ),
                    ),
                )
            )
        direction_method = (
            safe_sort_key.asc if normalized_direction == "ASC" else safe_sort_key.desc
        )
        id_direction = (
            LibraryItem.library_item_id.asc
            if normalized_direction == "ASC"
            else LibraryItem.library_item_id.desc
        )
        rows = (
            await session.execute(
                statement.add_columns(
                    safe_sort_key.label("catalogue_sort_key"),
                    null_rank.label("catalogue_null_rank"),
                )
                .order_by(null_rank.asc(), direction_method(), id_direction())
                .limit(limit + 1)
            )
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [row[0] for row in rows]
        collection_map = await self.collection_map(session, library_id, items)
        tag_map = await self.tag_map(session, library_id, items)
        pdf_map = await self.pdf_map(session, library_id, items)
        asset_map = await self.asset_map(session, library_id, items)
        artifact_summary_map = await self.artifact_summary_map(session, items)
        values = [
            await self.view(
                session,
                item,
                collection_ids=collection_map.get(item.library_item_id, []),
                tag_ids=tag_map.get(item.library_item_id, []),
                pdf_attachment=pdf_map.get(item.library_item_id),
                pdf_loaded=True,
                asset_attachments=asset_map.get(item.library_item_id, []),
                assets_loaded=True,
                artifact_summary=artifact_summary_map.get(item.library_item_id),
            )
            for item in items
        ]
        next_cursor = (
            self.encode_cursor(
                items[-1],
                sort=normalized_sort,
                direction=normalized_direction,
                null_rank=int(rows[-1][2]),
                value=rows[-1][1],
            )
            if has_more and items
            else None
        )
        return values, next_cursor

    @staticmethod
    def metadata_search_terms(query: str | None) -> list[str]:
        if query is None:
            return []
        # Bound the number of predicates generated by one request while preserving
        # type-ahead substring behavior for titles, people, and identifiers.
        return list(dict.fromkeys(query.casefold().split()))[:12]

    @staticmethod
    def metadata_search_value(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        return normalized or None

    @staticmethod
    async def expand_collection_ids(
        session: AsyncSession,
        library_id: uuid.UUID,
        roots: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        rows = (
            await session.execute(
                select(Collection.collection_id, Collection.parent_collection_id).where(
                    Collection.library_id == library_id,
                    Collection.status == "ACTIVE",
                )
            )
        ).all()
        children: dict[uuid.UUID, list[uuid.UUID]] = {}
        for collection_id, parent_id in rows:
            if parent_id is not None:
                children.setdefault(parent_id, []).append(collection_id)
        expanded = set(roots)
        pending = list(roots)
        while pending:
            for child_id in children.get(pending.pop(), []):
                if child_id not in expanded:
                    expanded.add(child_id)
                    pending.append(child_id)
        return list(expanded)

    async def get_item(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
    ) -> dict[str, object]:
        await membership_for(session, actor=actor, library_id=library_id)
        item = await self.require_item(session, library_id, library_item_id)
        collection_ids = await session.scalars(
            select(CollectionItem.collection_id).where(
                CollectionItem.library_id == library_id,
                CollectionItem.library_item_id == library_item_id,
            )
        )
        return await self.view(session, item, collection_ids=list(collection_ids))

    async def update_overrides(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        *,
        overrides: dict[str, Any],
        expected_revision: int,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        item = await self.require_item(session, library_id, library_item_id, lock=True)
        if item.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Library Item revision conflict")
        updated = dict(item.local_overrides)
        for key, value in self.normalize_overrides(overrides, allow_removal=True).items():
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
        item.local_overrides = updated
        item.revision += 1
        record_audit_event(
            session,
            "library.item_overrides_updated",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"library_item_id": str(library_item_id), "keys": sorted(overrides)},
        )
        await session.flush()
        await session.refresh(item)
        result = await self.view(session, item)
        await session.commit()
        return result

    async def update_item(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        *,
        overrides: dict[str, Any],
        collection_ids: list[uuid.UUID],
        tag_ids: list[uuid.UUID],
        expected_revision: int,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        item = await self.require_item(session, library_id, library_item_id, lock=True)
        if item.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Trashed Library Item cannot be edited")
        if item.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Library Item revision conflict")

        desired_collection_ids = list(dict.fromkeys(collection_ids))
        for collection_id in desired_collection_ids:
            await collection_service.require_active(session, library_id, collection_id)
        desired_tag_ids = list(dict.fromkeys(tag_ids))
        for tag_id in desired_tag_ids:
            await tag_service.require_active(session, library_id, tag_id)

        updated_overrides = dict(item.local_overrides)
        normalized = self.normalize_overrides(overrides, allow_removal=True)
        for key, value in normalized.items():
            if value is None:
                updated_overrides.pop(key, None)
            else:
                updated_overrides[key] = value

        current_collection_ids = set(
            await session.scalars(
                select(CollectionItem.collection_id).where(
                    CollectionItem.library_id == library_id,
                    CollectionItem.library_item_id == library_item_id,
                )
            )
        )
        desired_collection_id_set = set(desired_collection_ids)
        removed = current_collection_ids - desired_collection_id_set
        if removed:
            await session.execute(
                delete(CollectionItem).where(
                    CollectionItem.library_id == library_id,
                    CollectionItem.library_item_id == library_item_id,
                    CollectionItem.collection_id.in_(removed),
                )
            )
        for collection_id in desired_collection_id_set - current_collection_ids:
            session.add(
                CollectionItem(
                    library_id=library_id,
                    collection_id=collection_id,
                    library_item_id=library_item_id,
                    added_by=actor.principal_id,
                )
            )

        current_tag_ids = set(
            await session.scalars(
                select(ItemTag.tag_id).where(
                    ItemTag.library_id == library_id,
                    ItemTag.library_item_id == library_item_id,
                )
            )
        )
        desired_tag_id_set = set(desired_tag_ids)
        removed_tags = current_tag_ids - desired_tag_id_set
        if removed_tags:
            await session.execute(
                delete(ItemTag).where(
                    ItemTag.library_id == library_id,
                    ItemTag.library_item_id == library_item_id,
                    ItemTag.tag_id.in_(removed_tags),
                )
            )
        for tag_id in desired_tag_id_set - current_tag_ids:
            session.add(
                ItemTag(
                    library_id=library_id,
                    tag_id=tag_id,
                    library_item_id=library_item_id,
                    added_by=actor.principal_id,
                )
            )

        item.local_overrides = updated_overrides
        item.revision += 1
        record_audit_event(
            session,
            "library.item_updated",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={
                "library_item_id": str(library_item_id),
                "override_keys": sorted(overrides),
                "collection_ids": [str(value) for value in desired_collection_ids],
                "tag_ids": [str(value) for value in desired_tag_ids],
            },
        )
        await session.flush()
        await session.refresh(item)
        result = await self.view(
            session,
            item,
            collection_ids=desired_collection_ids,
            tag_ids=desired_tag_ids,
        )
        await session.commit()
        return result

    async def set_trash_state(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        *,
        trashed: bool,
        expected_revision: int,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        item = await self.require_item(session, library_id, library_item_id, lock=True)
        if item.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Library Item revision conflict")
        item.status = "TRASHED" if trashed else "ACTIVE"
        item.trashed_at = datetime.now(UTC) if trashed else None
        item.trashed_by = actor.principal_id if trashed else None
        item.revision += 1
        event_type = "library.item_trashed" if trashed else "library.item_restored"
        record_audit_event(
            session,
            event_type,
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={"library_item_id": str(library_item_id)},
        )
        await session.flush()
        await session.refresh(item)
        result = await self.view(session, item)
        await session.commit()
        return result

    async def bulk_organize(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        entries: list[tuple[uuid.UUID, int]],
        action: str,
        target_id: uuid.UUID | None,
    ) -> dict[str, object]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        entry_map = dict(entries)
        if len(entry_map) != len(entries):
            raise HTTPException(status_code=422, detail="duplicate Library Items in bulk request")
        item_ids = sorted(entry_map)
        items = list(
            await session.scalars(
                select(LibraryItem)
                .where(
                    LibraryItem.library_id == library_id,
                    LibraryItem.library_item_id.in_(item_ids),
                    LibraryItem.status != "PURGED",
                )
                .order_by(LibraryItem.library_item_id)
                .with_for_update()
            )
        )
        if len(items) != len(item_ids):
            raise HTTPException(status_code=404, detail="One or more Library Items were not found")
        for item in items:
            if item.revision != entry_map[item.library_item_id]:
                raise HTTPException(status_code=409, detail="Bulk Library Item revision conflict")

        if action in {"ADD_COLLECTION", "REMOVE_COLLECTION"}:
            if target_id is None:
                raise HTTPException(status_code=422, detail="Collection target is required")
            await collection_service.require_active(session, library_id, target_id)
            if action == "REMOVE_COLLECTION":
                await session.execute(
                    delete(CollectionItem).where(
                        CollectionItem.library_id == library_id,
                        CollectionItem.collection_id == target_id,
                        CollectionItem.library_item_id.in_(item_ids),
                    )
                )
            else:
                existing_ids = set(
                    await session.scalars(
                        select(CollectionItem.library_item_id).where(
                            CollectionItem.library_id == library_id,
                            CollectionItem.collection_id == target_id,
                            CollectionItem.library_item_id.in_(item_ids),
                        )
                    )
                )
                for item_id in set(item_ids) - existing_ids:
                    session.add(
                        CollectionItem(
                            library_id=library_id,
                            collection_id=target_id,
                            library_item_id=item_id,
                            added_by=actor.principal_id,
                        )
                    )
        elif action in {"ADD_TAG", "REMOVE_TAG"}:
            if target_id is None:
                raise HTTPException(status_code=422, detail="Tag target is required")
            await tag_service.require_active(session, library_id, target_id)
            if action == "REMOVE_TAG":
                await session.execute(
                    delete(ItemTag).where(
                        ItemTag.library_id == library_id,
                        ItemTag.tag_id == target_id,
                        ItemTag.library_item_id.in_(item_ids),
                    )
                )
            else:
                existing_ids = set(
                    await session.scalars(
                        select(ItemTag.library_item_id).where(
                            ItemTag.library_id == library_id,
                            ItemTag.tag_id == target_id,
                            ItemTag.library_item_id.in_(item_ids),
                        )
                    )
                )
                for item_id in set(item_ids) - existing_ids:
                    session.add(
                        ItemTag(
                            library_id=library_id,
                            tag_id=target_id,
                            library_item_id=item_id,
                            added_by=actor.principal_id,
                        )
                    )
        elif action in {"TRASH", "RESTORE"}:
            trashed = action == "TRASH"
            for item in items:
                expected_status = "ACTIVE" if trashed else "TRASHED"
                if item.status != expected_status:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Bulk {action.lower()} contains an Item in the wrong state",
                    )
                item.status = "TRASHED" if trashed else "ACTIVE"
                item.trashed_at = datetime.now(UTC) if trashed else None
                item.trashed_by = actor.principal_id if trashed else None
        else:
            raise HTTPException(status_code=422, detail="unsupported bulk action")

        for item in items:
            item.revision += 1
        record_audit_event(
            session,
            "library.items_bulk_organized",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={
                "action": action,
                "target_id": str(target_id) if target_id else None,
                "library_item_ids": [str(value) for value in item_ids],
            },
        )
        await session.commit()
        return {"updated": len(items), "action": action}

    async def resolve_canonical(
        self,
        session: AsyncSession,
        identifiers: list[tuple[str, str, str]],
    ) -> CanonicalPaper | None:
        return await paper_service.resolve(session, identifiers)

    async def create_canonical(
        self,
        session: AsyncSession,
        actor: Actor,
        *,
        metadata: dict[str, Any],
        identifiers: list[tuple[str, str, str]],
    ) -> CanonicalPaper:
        return await paper_service.create(
            session, actor, metadata=metadata, identifiers=identifiers
        )

    async def require_item(
        self,
        session: AsyncSession,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> LibraryItem:
        statement = select(LibraryItem).where(
            LibraryItem.library_id == library_id,
            LibraryItem.library_item_id == library_item_id,
            LibraryItem.status != "PURGED",
        )
        if lock:
            statement = statement.with_for_update()
        item = await session.scalar(statement)
        if item is None:
            raise HTTPException(status_code=404, detail="Library Item not found")
        return item

    async def view(
        self,
        session: AsyncSession,
        item: LibraryItem,
        *,
        collection_ids: list[uuid.UUID] | None = None,
        tag_ids: list[uuid.UUID] | None = None,
        pdf_attachment: dict[str, object] | None = None,
        pdf_loaded: bool = False,
        asset_attachments: list[dict[str, object]] | None = None,
        assets_loaded: bool = False,
        artifact_summary: dict[str, int] | None = None,
    ) -> dict[str, object]:
        paper = await session.get(CanonicalPaper, item.canonical_paper_id)
        if paper is None:
            raise HTTPException(status_code=500, detail="Canonical metadata is unavailable")
        metadata = await session.get(CanonicalMetadata, paper.canonical_paper_id)
        if metadata is None:
            raise HTTPException(status_code=500, detail="Canonical metadata is unavailable")
        baseline: dict[str, Any] = {
            "title": metadata.title,
            "abstract": metadata.abstract,
            "publication_year": metadata.publication_year,
            "publication_month": metadata.publication_month,
            "publication_day": metadata.publication_day,
            "publication_date": (
                metadata.publication_date.isoformat() if metadata.publication_date else None
            ),
            "publication_date_precision": metadata.publication_date_precision,
            "work_type": metadata.work_type,
            "venue": metadata.venue,
            "canonical_url": metadata.canonical_url,
            "publisher": metadata.publisher,
            "volume": metadata.volume,
            "issue": metadata.issue,
            "pages": metadata.pages,
            "article_number": metadata.article_number,
            "language": metadata.language,
            "issn": metadata.issn,
            "isbn": metadata.isbn,
            "authors": metadata.authors,
            "extra": metadata.extra,
        }
        effective = {**baseline, **item.local_overrides}
        identifiers = list(
            await session.scalars(
                select(CanonicalIdentifier).where(
                    CanonicalIdentifier.canonical_paper_id == item.canonical_paper_id
                )
            )
        )
        if collection_ids is None:
            collection_ids = list(
                await session.scalars(
                    select(CollectionItem.collection_id).where(
                        CollectionItem.library_id == item.library_id,
                        CollectionItem.library_item_id == item.library_item_id,
                    )
                )
            )
        if tag_ids is None:
            tag_ids = list(
                await session.scalars(
                    select(ItemTag.tag_id).where(
                        ItemTag.library_id == item.library_id,
                        ItemTag.library_item_id == item.library_item_id,
                    )
                )
            )
        if not pdf_loaded:
            pdf_attachment = (await self.pdf_map(session, item.library_id, [item])).get(
                item.library_item_id
            )
        if not assets_loaded:
            asset_attachments = (await self.asset_map(session, item.library_id, [item])).get(
                item.library_item_id, []
            )
        if artifact_summary is None:
            artifact_summary = (await self.artifact_summary_map(session, [item])).get(
                item.library_item_id,
                {"extracted_text": 0, "documents": 0},
            )
        return {
            "library_item_id": str(item.library_item_id),
            "library_id": str(item.library_id),
            "canonical_paper_id": str(item.canonical_paper_id),
            "status": item.status,
            "revision": item.revision,
            "metadata_source": metadata.metadata_source,
            "metadata_revision": metadata.revision,
            "canonical_metadata": baseline,
            "local_overrides": item.local_overrides,
            "effective_metadata": effective,
            "identifiers": [
                {"scheme": value.scheme, "value": value.original_value} for value in identifiers
            ],
            "collection_ids": [str(value) for value in collection_ids],
            "tag_ids": [str(value) for value in tag_ids],
            "pdf_attachment": pdf_attachment,
            "asset_attachments": asset_attachments or [],
            "resource_summary": {
                "primary_pdf": 1 if pdf_attachment is not None else 0,
                "extracted_text": artifact_summary.get("extracted_text", 0),
                "documents": artifact_summary.get("documents", 0),
                "assets": len(asset_attachments or []),
            },
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

    @staticmethod
    async def pdf_map(
        session: AsyncSession,
        library_id: uuid.UUID,
        items: list[LibraryItem],
    ) -> dict[uuid.UUID, dict[str, object]]:
        if not items:
            return {}
        item_ids = [item.library_item_id for item in items]
        paper_ids = [item.canonical_paper_id for item in items]
        overrides = {
            value.library_item_id: value
            for value in await session.scalars(
                select(ItemArtifactOverride).where(
                    ItemArtifactOverride.library_id == library_id,
                    ItemArtifactOverride.library_item_id.in_(item_ids),
                    ItemArtifactOverride.artifact_key == "pdf",
                )
            )
        }
        canonicals = {
            value.canonical_paper_id: value
            for value in await session.scalars(
                select(Artifact).where(
                    Artifact.canonical_paper_id.in_(paper_ids),
                    Artifact.artifact_key == "pdf",
                    Artifact.status == "ACTIVE",
                )
            )
        }
        result: dict[uuid.UUID, dict[str, object]] = {}
        for item in items:
            selected: ItemArtifactOverride | Artifact | None = overrides.get(item.library_item_id)
            origin = "OVERRIDE"
            if selected is None:
                selected = canonicals.get(item.canonical_paper_id)
                origin = "CANONICAL"
            if selected is None:
                continue
            result[item.library_item_id] = {
                "origin": origin,
                "artifact_type": selected.artifact_type,
                "filename": selected.original_filename,
                "media_type": selected.media_type,
                "revision": selected.revision,
            }
        return result

    @staticmethod
    async def asset_map(
        session: AsyncSession,
        library_id: uuid.UUID,
        items: list[LibraryItem],
    ) -> dict[uuid.UUID, list[dict[str, object]]]:
        if not items:
            return {}
        values = list(
            await session.scalars(
                select(Asset)
                .where(
                    Asset.library_id == library_id,
                    Asset.library_item_id.in_([item.library_item_id for item in items]),
                    Asset.status == "ACTIVE",
                )
                .order_by(Asset.created_at, Asset.asset_id)
            )
        )
        result: dict[uuid.UUID, list[dict[str, object]]] = {}
        for value in values:
            result.setdefault(value.library_item_id, []).append(
                {
                    "asset_id": str(value.asset_id),
                    "filename": value.display_name,
                    "media_type": value.media_type,
                    "revision": value.revision,
                }
            )
        return result

    @staticmethod
    async def artifact_summary_map(
        session: AsyncSession,
        items: list[LibraryItem],
    ) -> dict[uuid.UUID, dict[str, int]]:
        if not items:
            return {}
        artifacts = list(
            await session.scalars(
                select(Artifact).where(
                    Artifact.canonical_paper_id.in_([item.canonical_paper_id for item in items]),
                    Artifact.status.in_(("ACTIVE", "STALE")),
                    Artifact.artifact_type.in_(("EXTRACTED_TEXT", "PIPELINE_DOCUMENT")),
                )
            )
        )
        by_paper: dict[uuid.UUID, dict[str, int]] = {}
        for artifact in artifacts:
            counts = by_paper.setdefault(
                artifact.canonical_paper_id,
                {"extracted_text": 0, "documents": 0},
            )
            key = "documents" if artifact.artifact_type == "PIPELINE_DOCUMENT" else "extracted_text"
            counts[key] += 1
        return {
            item.library_item_id: by_paper.get(
                item.canonical_paper_id,
                {"extracted_text": 0, "documents": 0},
            )
            for item in items
        }

    @staticmethod
    async def collection_map(
        session: AsyncSession,
        library_id: uuid.UUID,
        items: list[LibraryItem],
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not items:
            return {}
        rows = (
            await session.execute(
                select(CollectionItem.library_item_id, CollectionItem.collection_id).where(
                    CollectionItem.library_id == library_id,
                    CollectionItem.library_item_id.in_([item.library_item_id for item in items]),
                )
            )
        ).all()
        result: dict[uuid.UUID, list[uuid.UUID]] = {}
        for item_id, collection_id in rows:
            result.setdefault(item_id, []).append(collection_id)
        return result

    @staticmethod
    async def tag_map(
        session: AsyncSession,
        library_id: uuid.UUID,
        items: list[LibraryItem],
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not items:
            return {}
        rows = (
            await session.execute(
                select(ItemTag.library_item_id, ItemTag.tag_id).where(
                    ItemTag.library_id == library_id,
                    ItemTag.library_item_id.in_([item.library_item_id for item in items]),
                )
            )
        ).all()
        result: dict[uuid.UUID, list[uuid.UUID]] = {}
        for item_id, tag_id in rows:
            result.setdefault(item_id, []).append(tag_id)
        return result

    @staticmethod
    def encode_cursor(
        item: LibraryItem,
        *,
        sort: str,
        direction: str,
        null_rank: int,
        value: object,
    ) -> str:
        serialized_value = value.isoformat() if isinstance(value, datetime) else value
        payload = json.dumps(
            {
                "v": 2,
                "sort": sort,
                "direction": direction,
                "null": null_rank,
                "value": serialized_value,
                "id": str(item.library_item_id),
            },
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def decode_cursor(
        value: str, *, sort: str, direction: str
    ) -> tuple[int, str | int | datetime, uuid.UUID]:
        try:
            padding = "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(value + padding))
            # Preserve cursors issued before advanced sorting was introduced.
            if isinstance(payload, list) and len(payload) == 2:
                if sort != "ADDED" or direction != "DESC":
                    raise ValueError
                created_at = datetime.fromisoformat(str(payload[0]))
                if created_at.tzinfo is None:
                    raise ValueError
                return 0, created_at, uuid.UUID(str(payload[1]))
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 2
                or payload.get("sort") != sort
                or payload.get("direction") != direction
                or payload.get("null") not in {0, 1}
            ):
                raise ValueError
            cursor_value: str | int | datetime
            if sort in {"ADDED", "MODIFIED"}:
                cursor_value = datetime.fromisoformat(str(payload["value"]))
                if cursor_value.tzinfo is None:
                    raise ValueError
            elif sort == "YEAR":
                cursor_value = int(payload["value"])
            else:
                cursor_value = str(payload["value"])
            return int(payload["null"]), cursor_value, uuid.UUID(str(payload["id"]))
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
            raise HTTPException(status_code=422, detail="invalid catalogue cursor") from error

    @staticmethod
    def normalize_identifiers(values: list[dict[str, str]]) -> list[tuple[str, str, str]]:
        return paper_service.normalize_identifiers(values)

    @staticmethod
    def normalize_overrides(values: dict[str, Any], *, allow_removal: bool) -> dict[str, Any]:
        unknown = set(values) - EDITABLE_METADATA_KEYS
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported metadata override keys: {', '.join(sorted(unknown))}",
            )
        result = dict(values)
        if not allow_removal and result.get("title") is not None:
            result["title"] = str(result["title"]).strip()
        return result

library_item_service = LibraryItemService()

# Transitional compatibility for older internal imports. New code should import
# ``library_item_service`` from the top-level ``library_items`` domain.
catalogue_service = library_item_service
