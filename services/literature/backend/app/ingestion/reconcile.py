from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    Artifact,
    Asset,
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    CollectionItem,
    ItemArtifactOverride,
    ItemTag,
    LibraryItem,
)

from .identifiers import ScholarlyIdentifier
from .providers import normalize_doi


@dataclass(frozen=True, slots=True)
class DoiReconciliation:
    library_item_id: uuid.UUID
    canonical_paper_id: uuid.UUID
    merged_item: bool
    metadata_already_resolved: bool


class IdentifierConflictError(ValueError):
    pass


class IdentifierReconciliationService:
    async def reconcile(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        doi: str,
        actor_principal_id: uuid.UUID | None,
    ) -> DoiReconciliation:
        normalized = normalize_doi(doi)
        return await self.reconcile_identifiers(
            session,
            library_id=library_id,
            library_item_id=library_item_id,
            identifiers=(
                ScholarlyIdentifier(
                    scheme="DOI",
                    normalized_value=normalized,
                    original_value=doi.strip(),
                    evidence="DOI_RECONCILIATION",
                ),
            ),
            actor_principal_id=actor_principal_id,
        )

    async def reconcile_identifiers(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        identifiers: tuple[ScholarlyIdentifier, ...],
        actor_principal_id: uuid.UUID | None,
    ) -> DoiReconciliation:
        unique = {(value.scheme, value.normalized_value): value for value in identifiers}
        if not unique:
            raise ValueError("At least one scholarly identifier is required")
        for scheme, normalized in sorted(unique):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"identifier:{scheme}:{normalized}"},
            )
        incoming_item = await session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.library_id == library_id,
                LibraryItem.library_item_id == library_item_id,
                LibraryItem.status != "PURGED",
            )
            .with_for_update()
        )
        if incoming_item is None:
            raise LookupError("Library Item not found")

        matched_identifiers = list(
            await session.scalars(
                select(CanonicalIdentifier).where(
                    or_(
                        *[
                            (CanonicalIdentifier.scheme == scheme)
                            & (CanonicalIdentifier.normalized_value == normalized)
                            for scheme, normalized in unique
                        ]
                    )
                )
            )
        )
        matched_paper_ids = {value.canonical_paper_id for value in matched_identifiers}
        if len(matched_paper_ids) > 1:
            raise IdentifierConflictError(
                "Extracted identifiers resolve to different Canonical Papers"
            )

        target_paper_id = (
            next(iter(matched_paper_ids)) if matched_paper_ids else incoming_item.canonical_paper_id
        )
        if target_paper_id == incoming_item.canonical_paper_id:
            await self._attach_missing_identifiers(
                session,
                canonical_paper_id=target_paper_id,
                identifiers=tuple(unique.values()),
            )
            await session.flush()
            return await self._result(session, incoming_item, merged_item=False)

        provisional_paper_id = incoming_item.canonical_paper_id
        target_paper = await session.scalar(
            select(CanonicalPaper)
            .where(CanonicalPaper.canonical_paper_id == target_paper_id)
            .with_for_update()
        )
        if target_paper is None or target_paper.status != "ACTIVE":
            raise ValueError("Matched Canonical Paper is not active")

        target_item = await session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.library_id == library_id,
                LibraryItem.canonical_paper_id == target_paper_id,
                LibraryItem.status != "PURGED",
            )
            .with_for_update()
        )
        if target_item is None:
            await self._move_item_to_paper(
                session,
                incoming_item,
                target_paper_id=target_paper_id,
                actor_principal_id=actor_principal_id,
            )
            winner = incoming_item
            merged_item = False
        else:
            await self._merge_items(
                session,
                incoming=incoming_item,
                target=target_item,
                actor_principal_id=actor_principal_id,
            )
            winner = target_item
            merged_item = True

        await self._move_provisional_artifacts(
            session,
            provisional_paper_id=provisional_paper_id,
            target=winner,
            actor_principal_id=actor_principal_id,
        )
        remaining = await session.scalar(
            select(func.count())
            .select_from(LibraryItem)
            .where(LibraryItem.canonical_paper_id == provisional_paper_id)
        )
        if remaining == 0:
            provisional = await session.get(CanonicalPaper, provisional_paper_id)
            if provisional is not None:
                await session.delete(provisional)
        await self._attach_missing_identifiers(
            session,
            canonical_paper_id=target_paper_id,
            identifiers=tuple(unique.values()),
        )
        await session.flush()
        return await self._result(session, winner, merged_item=merged_item)

    @staticmethod
    async def _attach_missing_identifiers(
        session: AsyncSession,
        *,
        canonical_paper_id: uuid.UUID,
        identifiers: tuple[ScholarlyIdentifier, ...],
    ) -> None:
        existing = set(
            (
                await session.execute(
                    select(
                        CanonicalIdentifier.scheme,
                        CanonicalIdentifier.normalized_value,
                    ).where(CanonicalIdentifier.canonical_paper_id == canonical_paper_id)
                )
            ).tuples()
        )
        for value in identifiers:
            key = (value.scheme, value.normalized_value)
            if key in existing:
                continue
            session.add(
                CanonicalIdentifier(
                    canonical_paper_id=canonical_paper_id,
                    scheme=value.scheme,
                    normalized_value=value.normalized_value,
                    original_value=value.original_value,
                )
            )
            existing.add(key)

    @staticmethod
    async def _move_item_to_paper(
        session: AsyncSession,
        item: LibraryItem,
        *,
        target_paper_id: uuid.UUID,
        actor_principal_id: uuid.UUID | None,
    ) -> None:
        overrides = list(
            await session.scalars(
                select(ItemArtifactOverride).where(
                    ItemArtifactOverride.library_id == item.library_id,
                    ItemArtifactOverride.library_item_id == item.library_item_id,
                )
            )
        )
        snapshots = [
            {
                "artifact_key": value.artifact_key,
                "artifact_type": value.artifact_type,
                "blob_id": value.blob_id,
                "original_filename": value.original_filename,
                "media_type": value.media_type,
                "provenance": value.provenance,
                "source_fingerprint": value.source_fingerprint,
                "revision": value.revision,
                "specified_by": value.specified_by,
            }
            for value in overrides
        ]
        for override in overrides:
            await session.delete(override)
        await session.flush()
        item.canonical_paper_id = target_paper_id
        item.revision += 1
        await session.flush()
        for value in snapshots:
            session.add(
                ItemArtifactOverride(
                    library_id=item.library_id,
                    library_item_id=item.library_item_id,
                    canonical_paper_id=target_paper_id,
                    specified_by=value.pop("specified_by") or actor_principal_id,
                    **value,
                )
            )

    async def _merge_items(
        self,
        session: AsyncSession,
        *,
        incoming: LibraryItem,
        target: LibraryItem,
        actor_principal_id: uuid.UUID | None,
    ) -> None:
        await self._merge_collections(session, incoming=incoming, target=target)
        await self._merge_tags(session, incoming=incoming, target=target)
        await session.execute(
            update(Asset)
            .where(
                Asset.library_id == incoming.library_id,
                Asset.library_item_id == incoming.library_item_id,
            )
            .values(library_item_id=target.library_item_id)
        )
        incoming_overrides = list(
            await session.scalars(
                select(ItemArtifactOverride).where(
                    ItemArtifactOverride.library_id == incoming.library_id,
                    ItemArtifactOverride.library_item_id == incoming.library_item_id,
                )
            )
        )
        target_keys = set(
            await session.scalars(
                select(ItemArtifactOverride.artifact_key).where(
                    ItemArtifactOverride.library_id == target.library_id,
                    ItemArtifactOverride.library_item_id == target.library_item_id,
                )
            )
        )
        for override in incoming_overrides:
            if override.artifact_key in target_keys:
                session.add(
                    Asset(
                        library_id=target.library_id,
                        library_item_id=target.library_item_id,
                        blob_id=override.blob_id,
                        display_name=override.original_filename
                        or f"Imported {override.artifact_key}",
                        media_type=override.media_type,
                        status="ACTIVE",
                        provenance={
                            **override.provenance,
                            "merge_conflict": "EXISTING_ARTIFACT_SELECTION_PRESERVED",
                            "artifact_key": override.artifact_key,
                        },
                        revision=1,
                        created_by=actor_principal_id,
                    )
                )
            else:
                session.add(
                    ItemArtifactOverride(
                        library_id=target.library_id,
                        library_item_id=target.library_item_id,
                        canonical_paper_id=target.canonical_paper_id,
                        artifact_key=override.artifact_key,
                        artifact_type=override.artifact_type,
                        blob_id=override.blob_id,
                        original_filename=override.original_filename,
                        media_type=override.media_type,
                        provenance=override.provenance,
                        source_fingerprint=override.source_fingerprint,
                        revision=override.revision,
                        specified_by=override.specified_by or actor_principal_id,
                    )
                )
                target_keys.add(override.artifact_key)
            await session.delete(override)
        target.local_overrides = {**incoming.local_overrides, **target.local_overrides}
        target.revision += 1
        await session.delete(incoming)
        await session.flush()

    @staticmethod
    async def _merge_collections(
        session: AsyncSession, *, incoming: LibraryItem, target: LibraryItem
    ) -> None:
        incoming_ids = set(
            await session.scalars(
                select(CollectionItem.collection_id).where(
                    CollectionItem.library_id == incoming.library_id,
                    CollectionItem.library_item_id == incoming.library_item_id,
                )
            )
        )
        target_ids = set(
            await session.scalars(
                select(CollectionItem.collection_id).where(
                    CollectionItem.library_id == target.library_id,
                    CollectionItem.library_item_id == target.library_item_id,
                )
            )
        )
        for collection_id in incoming_ids - target_ids:
            session.add(
                CollectionItem(
                    library_id=target.library_id,
                    collection_id=collection_id,
                    library_item_id=target.library_item_id,
                    added_by=target.saved_by,
                )
            )

    @staticmethod
    async def _merge_tags(
        session: AsyncSession, *, incoming: LibraryItem, target: LibraryItem
    ) -> None:
        incoming_ids = set(
            await session.scalars(
                select(ItemTag.tag_id).where(
                    ItemTag.library_id == incoming.library_id,
                    ItemTag.library_item_id == incoming.library_item_id,
                )
            )
        )
        target_ids = set(
            await session.scalars(
                select(ItemTag.tag_id).where(
                    ItemTag.library_id == target.library_id,
                    ItemTag.library_item_id == target.library_item_id,
                )
            )
        )
        for tag_id in incoming_ids - target_ids:
            session.add(
                ItemTag(
                    library_id=target.library_id,
                    tag_id=tag_id,
                    library_item_id=target.library_item_id,
                    added_by=target.saved_by,
                )
            )

    @staticmethod
    async def _move_provisional_artifacts(
        session: AsyncSession,
        *,
        provisional_paper_id: uuid.UUID,
        target: LibraryItem,
        actor_principal_id: uuid.UUID | None,
    ) -> None:
        artifacts = list(
            await session.scalars(
                select(Artifact).where(Artifact.canonical_paper_id == provisional_paper_id)
            )
        )
        existing_overrides = {
            value.artifact_key: value
            for value in await session.scalars(
                select(ItemArtifactOverride).where(
                    ItemArtifactOverride.library_id == target.library_id,
                    ItemArtifactOverride.library_item_id == target.library_item_id,
                )
            )
        }
        canonical_keys = set(
            await session.scalars(
                select(Artifact.artifact_key).where(
                    Artifact.canonical_paper_id == target.canonical_paper_id
                )
            )
        )
        for artifact in artifacts:
            if artifact.artifact_key not in canonical_keys:
                artifact.canonical_paper_id = target.canonical_paper_id
                canonical_keys.add(artifact.artifact_key)
                continue
            existing_override = existing_overrides.get(artifact.artifact_key)
            if existing_override is not None and existing_override.blob_id == artifact.blob_id:
                await session.delete(artifact)
            elif existing_override is not None:
                session.add(
                    Asset(
                        library_id=target.library_id,
                        library_item_id=target.library_item_id,
                        blob_id=artifact.blob_id,
                        display_name=artifact.original_filename
                        or f"Imported {artifact.artifact_key}",
                        media_type=artifact.media_type,
                        status="ACTIVE",
                        provenance={
                            **artifact.provenance,
                            "merge_conflict": "EXISTING_ARTIFACT_SELECTION_PRESERVED",
                            "artifact_key": artifact.artifact_key,
                        },
                        revision=1,
                        created_by=actor_principal_id,
                    )
                )
                await session.delete(artifact)
            else:
                session.add(
                    ItemArtifactOverride(
                        library_id=target.library_id,
                        library_item_id=target.library_item_id,
                        canonical_paper_id=target.canonical_paper_id,
                        artifact_key=artifact.artifact_key,
                        artifact_type=artifact.artifact_type,
                        blob_id=artifact.blob_id,
                        original_filename=artifact.original_filename,
                        media_type=artifact.media_type,
                        provenance=artifact.provenance,
                        source_fingerprint=artifact.source_fingerprint,
                        revision=1,
                        specified_by=actor_principal_id,
                    )
                )
                await session.delete(artifact)

    @staticmethod
    async def _result(
        session: AsyncSession, item: LibraryItem, *, merged_item: bool
    ) -> DoiReconciliation:
        metadata = await session.get(CanonicalMetadata, item.canonical_paper_id)
        return DoiReconciliation(
            library_item_id=item.library_item_id,
            canonical_paper_id=item.canonical_paper_id,
            merged_item=merged_item,
            metadata_already_resolved=(
                metadata is not None
                and metadata.metadata_source in {"CROSSREF", "OPENALEX", "ARXIV", "ZOTERO"}
            ),
        )


identifier_reconciliation_service = IdentifierReconciliationService()
doi_reconciliation_service = identifier_reconciliation_service
