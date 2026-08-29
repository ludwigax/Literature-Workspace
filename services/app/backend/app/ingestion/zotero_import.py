from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import ObjectStorage
from backend.app.authorization.dependencies import Actor
from backend.app.jobs.service import job_service
from backend.app.models import (
    BackgroundJob,
    Blob,
    CanonicalMetadata,
    Collection,
    LibraryItem,
    Principal,
    ZoteroCollectionMapping,
    ZoteroImportEntry,
    ZoteroImportSource,
)

from .limited_items import limited_item_service
from .zotero_snapshot import (
    ZOTERO_IMPORT_JOB,
    ZoteroItemRecord,
    ZoteroSnapshot,
    parse_zotero_snapshot,
)


def _same_zotero_attachment(
    incoming: dict[str, Any], existing: dict[str, Any]
) -> bool:
    if str(incoming.get("item_key") or "") != str(existing.get("item_key") or ""):
        return False
    incoming_hash = str(incoming.get("storage_hash") or "").strip().casefold()
    existing_hash = str(existing.get("storage_hash") or "").strip().casefold()
    if incoming_hash or existing_hash:
        return bool(incoming_hash and existing_hash and incoming_hash == existing_hash)
    incoming_version = incoming.get("version")
    existing_version = existing.get("version")
    same_path = str(incoming.get("path") or "").replace("\\", "/") == str(
        existing.get("path") or ""
    ).replace("\\", "/")
    if incoming_version is not None and existing_version is not None:
        return same_path and int(incoming_version) == int(existing_version)
    return (
        same_path
        and incoming.get("link_mode") == existing.get("link_mode")
        and str(incoming.get("content_type") or "").casefold()
        == str(existing.get("content_type") or "").casefold()
    )


def merge_attachment_manifest(
    incoming: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep upload state only for an unchanged Zotero attachment declaration."""
    existing_by_key = {
        str(value.get("item_key") or ""): value
        for value in existing
        if value.get("item_key")
    }
    merged: list[dict[str, Any]] = []
    for declaration in incoming:
        value = dict(declaration)
        previous = existing_by_key.get(str(value.get("item_key") or ""))
        if (
            previous is not None
            and previous.get("file_available") is True
            and previous.get("blob_id")
            and _same_zotero_attachment(value, previous)
        ):
            value.update(
                file_available=True,
                blob_id=previous["blob_id"],
                import_role=previous.get("import_role"),
            )
        else:
            value["file_available"] = False
            value.pop("blob_id", None)
            value.pop("import_role", None)
        merged.append(value)
    return merged


class ZoteroImportHandler:
    job_type = ZOTERO_IMPORT_JOB

    def __init__(self, *, max_bytes: int, storage: ObjectStorage) -> None:
        self.max_bytes = max_bytes
        self.storage = storage

    async def handle(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> None:
        blob = await session.get(Blob, uuid.UUID(str(job.payload["blob_id"])))
        if blob is None or blob.status != "AVAILABLE":
            raise LookupError("Zotero snapshot Blob is unavailable")
        data = await self.storage.read_bytes(blob.storage_key, self.max_bytes)
        snapshot = await asyncio.to_thread(parse_zotero_snapshot, data)
        principal = (
            await session.get(Principal, job.actor_principal_id)
            if job.actor_principal_id is not None
            else None
        )
        if principal is None:
            raise LookupError("Zotero import actor is unavailable")
        actor = Actor(
            principal_id=principal.principal_id,
            display_name=principal.display_name,
            session_id=job.correlation_id,
        )
        source = await self._source(session, job, snapshot)
        collection_ids = await self._collections(
            session,
            job=job,
            actor=actor,
            source=source,
            snapshot=snapshot,
        )
        total = len(snapshot.items)
        await job_service.progress(
            session,
            job.job_id,
            worker_id=worker_id,
            current=min(job.progress_current, total),
            total=max(1, total),
            message=f"Importing {total} Zotero records",
            lease_seconds=180,
        )
        await session.commit()

        created = updated = preserved = conflicts = attachment_declarations = 0
        for index, record in enumerate(snapshot.items, start=1):
            try:
                initialized = await limited_item_service.initialize(
                    session,
                    actor=actor,
                    library_id=job.library_id,
                    metadata=record.metadata,
                    doi=None,
                    identifiers=list(record.identifiers),
                    collection_ids=[
                        collection_ids[(record.zotero_library_id, key)]
                        for key in record.collection_keys
                        if (record.zotero_library_id, key) in collection_ids
                    ],
                )
            except (HTTPException, ValueError):
                conflicts += 1
                continue

            metadata = await session.get(CanonicalMetadata, initialized.item.canonical_paper_id)
            if metadata is None:
                raise RuntimeError("Zotero Item has no Canonical Metadata")
            if metadata.metadata_source == "UNDEFINED":
                self._apply_metadata(metadata, record.metadata, actor.principal_id)
                if initialized.created:
                    created += 1
                else:
                    updated += 1
            else:
                preserved += 1
            attachment_declarations += len(record.attachments)
            await self._map_entry(
                session,
                source=source,
                item=initialized.item,
                record=record,
            )
            if index % 10 == 0 or index == total:
                await job_service.progress(
                    session,
                    job.job_id,
                    worker_id=worker_id,
                    current=index,
                    total=max(1, total),
                    message=f"Imported {index} of {total} Zotero records",
                    lease_seconds=180,
                )
                await session.commit()

        source.last_imported_at = datetime.now(UTC)
        await job_service.succeed(
            session,
            job.job_id,
            worker_id=worker_id,
            result={
                "source_id": str(source.source_id),
                "source_identity": snapshot.source_identity,
                "schema_version": snapshot.schema_version,
                "record_count": total,
                "created_or_upgraded": created + updated,
                "authoritative_metadata_preserved": preserved,
                "identifier_conflicts": conflicts,
                "collection_count": len(snapshot.collections),
                "attachment_declarations": attachment_declarations,
                "attachment_files_imported": 0,
            },
        )

    @staticmethod
    async def _source(
        session: AsyncSession,
        job: BackgroundJob,
        snapshot: ZoteroSnapshot,
    ) -> ZoteroImportSource:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"zotero:{job.library_id}:{snapshot.source_identity}"},
        )
        source = await session.scalar(
            select(ZoteroImportSource).where(
                ZoteroImportSource.library_id == job.library_id,
                ZoteroImportSource.source_identity == snapshot.source_identity,
            )
        )
        if source is None:
            source = ZoteroImportSource(
                library_id=job.library_id,
                source_identity=snapshot.source_identity,
                display_name=snapshot.display_name,
            )
            session.add(source)
            await session.flush()
        else:
            source.display_name = snapshot.display_name
        return source

    @staticmethod
    async def _collections(
        session: AsyncSession,
        *,
        job: BackgroundJob,
        actor: Actor,
        source: ZoteroImportSource,
        snapshot: ZoteroSnapshot,
    ) -> dict[tuple[int, str], uuid.UUID]:
        mappings = list(
            await session.scalars(
                select(ZoteroCollectionMapping).where(
                    ZoteroCollectionMapping.source_id == source.source_id
                )
            )
        )
        result = {
            (value.zotero_library_id, value.collection_key): value.collection_id
            for value in mappings
        }
        pending = list(snapshot.collections)
        while pending:
            progressed = False
            for value in pending[:]:
                key = (value.zotero_library_id, value.key)
                if key in result:
                    pending.remove(value)
                    progressed = True
                    continue
                parent_id = (
                    result.get((value.zotero_library_id, value.parent_key))
                    if value.parent_key
                    else None
                )
                if value.parent_key and parent_id is None:
                    continue
                collection = Collection(
                    library_id=job.library_id,
                    parent_collection_id=parent_id,
                    name=value.name[:200],
                    status="ACTIVE",
                    revision=1,
                    created_by=actor.principal_id,
                )
                session.add(collection)
                await session.flush()
                session.add(
                    ZoteroCollectionMapping(
                        source_id=source.source_id,
                        zotero_library_id=value.zotero_library_id,
                        collection_key=value.key,
                        library_id=job.library_id,
                        collection_id=collection.collection_id,
                    )
                )
                result[key] = collection.collection_id
                pending.remove(value)
                progressed = True
            if not progressed:
                raise ValueError("Zotero Collection hierarchy contains a cycle or missing parent")
        return result

    @staticmethod
    def _apply_metadata(
        current: CanonicalMetadata,
        incoming: dict[str, Any],
        actor_principal_id: uuid.UUID,
    ) -> None:
        provenance = dict(incoming.get("provenance") or {})
        current.metadata_source = "ZOTERO"
        current.source_record_id = str(provenance.get("zotero_item_key"))
        current.title = str(incoming["title"])
        current.abstract = incoming.get("abstract")
        current.publication_year = incoming.get("publication_year")
        current.publication_month = incoming.get("publication_month")
        current.publication_day = incoming.get("publication_day")
        publication_date = incoming.get("publication_date")
        current.publication_date = (
            date.fromisoformat(str(publication_date)) if publication_date else None
        )
        current.publication_date_precision = incoming.get("publication_date_precision")
        current.work_type = incoming.get("work_type")
        current.venue = incoming.get("venue")
        current.canonical_url = incoming.get("canonical_url")
        current.publisher = incoming.get("publisher")
        current.volume = incoming.get("volume")
        current.issue = incoming.get("issue")
        current.pages = incoming.get("pages")
        current.article_number = incoming.get("article_number")
        current.language = incoming.get("language")
        current.issn = list(incoming.get("issn") or [])
        current.isbn = list(incoming.get("isbn") or [])
        current.authors = list(incoming.get("authors") or [])
        current.extra = dict(incoming.get("extra") or {})
        current.provenance = provenance
        current.revision += 1
        current.updated_by = actor_principal_id

    @staticmethod
    async def _map_entry(
        session: AsyncSession,
        *,
        source: ZoteroImportSource,
        item: LibraryItem,
        record: ZoteroItemRecord,
    ) -> None:
        zotero_library_id = record.zotero_library_id
        key = record.key
        mapping = await session.get(
            ZoteroImportEntry,
            {
                "source_id": source.source_id,
                "zotero_library_id": zotero_library_id,
                "item_key": key,
            },
        )
        attachment_manifest = (
            list(record.attachments)
            if mapping is None
            else merge_attachment_manifest(
                record.attachments, list(mapping.attachment_manifest)
            )
        )
        values = {
            "library_id": item.library_id,
            "library_item_id": item.library_item_id,
            "item_version": record.version,
            "item_type": record.item_type,
            "attachment_manifest": attachment_manifest,
        }
        if mapping is None:
            session.add(
                ZoteroImportEntry(
                    source_id=source.source_id,
                    zotero_library_id=zotero_library_id,
                    item_key=key,
                    **values,
                )
            )
        else:
            for field, value in values.items():
                setattr(mapping, field, value)
