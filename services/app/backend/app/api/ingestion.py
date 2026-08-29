from __future__ import annotations

import uuid
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from backend.app.assets.service import artifact_service
from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import get_object_storage
from backend.app.audit import record_audit_event
from backend.app.authorization.dependencies import (
    CsrfProtected,
    CurrentActor,
    Database,
    membership_for,
)
from backend.app.catalogue.service import catalogue_service
from backend.app.collections.service import collection_service
from backend.app.config import get_settings
from backend.app.ingestion.citation_import import CITATION_IMPORT_JOB
from backend.app.ingestion.limited_items import limited_item_service
from backend.app.ingestion.pdf_import import PDF_IMPORT_JOB
from backend.app.ingestion.service import metadata_refresh_service
from backend.app.ingestion.zotero_snapshot import ZOTERO_IMPORT_JOB
from backend.app.jobs.service import job_service
from backend.app.models import (
    Asset,
    BackgroundJob,
    LibraryItem,
    ZoteroImportEntry,
)

router = APIRouter(prefix="/libraries", tags=["ingestion"])


class MetadataRefreshBody(BaseModel):
    library_item_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    request_id: uuid.UUID
    refresh_mode: Literal["AUTO", "MANUAL"] = "MANUAL"


@router.post("/{library_id}/imports/zotero", status_code=202)
async def import_zotero(
    library_id: uuid.UUID,
    request: Request,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    filename: str = Query(min_length=1, max_length=1024),
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    data = await request.body()
    limit = get_settings().zotero_import_max_bytes
    if not data:
        raise HTTPException(status_code=422, detail="Zotero database is empty")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="Zotero database exceeds the upload limit")
    if not data.startswith(b"SQLite format 3\x00"):
        raise HTTPException(status_code=422, detail="Uploaded file is not a SQLite database")
    storage = get_object_storage()
    await storage.ensure_bucket()
    blob = await blob_service.store_bytes(
        session,
        storage,
        data=data,
        media_type="application/vnd.sqlite3",
        actor_principal_id=actor.principal_id,
    )
    job = await job_service.enqueue(
        session,
        actor,
        library_id,
        job_type=ZOTERO_IMPORT_JOB,
        payload={"blob_id": str(blob.blob_id), "filename": filename},
        idempotency_key=f"zotero-snapshot:{blob.blob_id}",
        progress_total=1,
        max_attempts=2,
    )
    record_audit_event(
        session,
        "library.zotero_import_requested",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={"job_id": str(job.job_id), "blob_id": str(blob.blob_id)},
    )
    await session.commit()
    return {"job_id": str(job.job_id), "status": job.status, "filename": filename}


async def _zotero_source_id(
    session: Database,
    *,
    library_id: uuid.UUID,
    job_id: uuid.UUID,
) -> uuid.UUID:
    job = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.library_id == library_id,
            BackgroundJob.job_id == job_id,
            BackgroundJob.job_type == ZOTERO_IMPORT_JOB,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Zotero import job not found")
    if job.status != "SUCCEEDED" or not job.result:
        raise HTTPException(status_code=409, detail="Zotero database import is not complete")
    try:
        return uuid.UUID(str(job.result["source_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=409,
            detail="Zotero import predates attachment-folder support; import it again",
        ) from error


def _zotero_pdf_declarations(entry: ZoteroImportEntry) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for attachment in entry.attachment_manifest:
        path = str(attachment.get("path") or "")
        content_type = str(attachment.get("content_type") or "")
        if not path.casefold().startswith("storage:"):
            continue
        filename = path.split(":", 1)[1].replace("\\", "/").rsplit("/", 1)[-1]
        if content_type.casefold() != "application/pdf" and not filename.casefold().endswith(
            ".pdf"
        ):
            continue
        result.append(
            {
                **attachment,
                "filename": filename,
                "relative_path": f"storage/{attachment['item_key']}/{filename}",
            }
        )
    return result


@router.get("/{library_id}/imports/zotero/{job_id}/attachments")
async def zotero_attachment_manifest(
    library_id: uuid.UUID,
    job_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
) -> dict[str, object]:
    await membership_for(session, actor=actor, library_id=library_id)
    source_id = await _zotero_source_id(session, library_id=library_id, job_id=job_id)
    entries = list(
        await session.scalars(
            select(ZoteroImportEntry).where(
                ZoteroImportEntry.source_id == source_id,
                ZoteroImportEntry.library_id == library_id,
            )
        )
    )
    attachments: list[dict[str, object]] = []
    for entry in entries:
        for index, value in enumerate(_zotero_pdf_declarations(entry)):
            attachments.append(
                {
                    "source_id": str(source_id),
                    "zotero_library_id": entry.zotero_library_id,
                    "item_key": entry.item_key,
                    "attachment_key": value["item_key"],
                    "library_item_id": str(entry.library_item_id),
                    "filename": value["filename"],
                    "relative_path": value["relative_path"],
                    "role": "PRIMARY_PDF" if index == 0 else "ASSET",
                    "imported": bool(value.get("file_available")),
                }
            )
    return {"source_id": str(source_id), "attachments": attachments}


@router.post("/{library_id}/imports/zotero/{job_id}/attachments/{attachment_key}")
async def import_zotero_attachment(
    library_id: uuid.UUID,
    job_id: uuid.UUID,
    attachment_key: str,
    request: Request,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    zotero_library_id: int,
    item_key: str = Query(min_length=1, max_length=32),
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    source_id = await _zotero_source_id(session, library_id=library_id, job_id=job_id)
    entry = await session.get(
        ZoteroImportEntry,
        {
            "source_id": source_id,
            "zotero_library_id": zotero_library_id,
            "item_key": item_key,
        },
    )
    if entry is None or entry.library_id != library_id:
        raise HTTPException(status_code=404, detail="Zotero Item mapping not found")
    declarations = _zotero_pdf_declarations(entry)
    selected = next(
        (value for value in declarations if str(value["item_key"]) == attachment_key),
        None,
    )
    if selected is None:
        raise HTTPException(status_code=404, detail="Zotero PDF declaration not found")
    data = await request.body()
    limit = get_settings().pdf_import_max_bytes
    if not data:
        raise HTTPException(status_code=422, detail="Zotero PDF is empty")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="Zotero PDF exceeds the upload limit")
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(status_code=422, detail="Zotero attachment is not a PDF")

    storage = get_object_storage()
    await storage.ensure_bucket()
    blob = await blob_service.store_bytes(
        session,
        storage,
        data=data,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
    )
    item = await session.scalar(
        select(LibraryItem)
        .where(
            LibraryItem.library_id == library_id,
            LibraryItem.library_item_id == entry.library_item_id,
            LibraryItem.status != "PURGED",
        )
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Mapped Library Item is unavailable")
    role = "PRIMARY_PDF" if declarations[0]["item_key"] == attachment_key else "ASSET"
    filename = str(selected["filename"])
    promoted = False
    provenance = {
        "source": "zotero_folder",
        "zotero_source_id": str(source_id),
        "zotero_library_id": zotero_library_id,
        "zotero_item_key": item_key,
        "zotero_attachment_key": attachment_key,
        "blob_sha256": blob.sha256,
    }
    if role == "PRIMARY_PDF":
        await artifact_service.specify_for_item(
            session,
            library_id=library_id,
            library_item_id=item.library_item_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=blob.blob_id,
            media_type="application/pdf",
            actor_principal_id=actor.principal_id,
            original_filename=filename,
            provenance={**provenance, "selection": "USER_OVERRIDE"},
        )
        promotion = await artifact_service.set_canonical_if_missing(
            session,
            canonical_paper_id=item.canonical_paper_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=blob.blob_id,
            media_type="application/pdf",
            actor_principal_id=actor.principal_id,
            original_filename=filename,
            provenance={
                **provenance,
                "verification_status": "UNVERIFIED",
                "promoted_from_library_item_id": str(item.library_item_id),
            },
            verification_status="UNVERIFIED",
        )
        promoted = promotion[1]
    else:
        existing_assets = list(
            await session.scalars(
                select(Asset).where(
                    Asset.library_id == library_id,
                    Asset.library_item_id == item.library_item_id,
                    Asset.status == "ACTIVE",
                )
            )
        )
        existing_asset = next(
            (
                value
                for value in existing_assets
                if value.provenance.get("zotero_source_id") == str(source_id)
                and value.provenance.get("zotero_attachment_key") == attachment_key
            ),
            None,
        )
        if existing_asset is None:
            session.add(
                Asset(
                    library_id=library_id,
                    library_item_id=item.library_item_id,
                    blob_id=blob.blob_id,
                    display_name=filename,
                    media_type="application/pdf",
                    status="ACTIVE",
                    provenance=provenance,
                    revision=1,
                    created_by=actor.principal_id,
                )
            )

    updated_manifest: list[dict[str, object]] = []
    for value in entry.attachment_manifest:
        updated = dict(value)
        if str(updated.get("item_key")) == attachment_key:
            updated.update(
                {
                    "file_available": True,
                    "blob_id": str(blob.blob_id),
                    "import_role": role,
                }
            )
        updated_manifest.append(updated)
    entry.attachment_manifest = updated_manifest
    record_audit_event(
        session,
        "library.zotero_attachment_imported",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={
            "library_item_id": str(item.library_item_id),
            "attachment_key": attachment_key,
            "role": role,
            "canonical_promoted": promoted,
        },
    )
    await session.commit()
    return {
        "library_item_id": str(item.library_item_id),
        "attachment_key": attachment_key,
        "role": role,
        "blob_id": str(blob.blob_id),
        "canonical_promoted": promoted,
    }


@router.post("/{library_id}/imports/pdfs", status_code=202)
async def import_pdf(
    library_id: uuid.UUID,
    request: Request,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    filename: str = Query(min_length=1, max_length=1024),
    collection_id: uuid.UUID | None = None,
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    if collection_id is not None:
        await collection_service.require_active(session, library_id, collection_id)
    data = await request.body()
    limit = get_settings().pdf_import_max_bytes
    if not data:
        raise HTTPException(status_code=422, detail="PDF is empty")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="PDF exceeds the upload limit")
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(status_code=422, detail="Uploaded file is not a PDF")

    storage = get_object_storage()
    await storage.ensure_bucket()
    blob = await blob_service.store_bytes(
        session,
        storage,
        data=data,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
    )
    idempotency_key = f"pdf-blob:{blob.blob_id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"pdf-upload:{library_id}:{blob.blob_id}"},
    )
    existing_job = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.library_id == library_id,
            BackgroundJob.job_type == PDF_IMPORT_JOB,
            BackgroundJob.idempotency_key == idempotency_key,
        )
    )
    if existing_job is not None:
        existing_item_id = uuid.UUID(
            str(
                (existing_job.result or {}).get("library_item_id")
                or existing_job.payload["library_item_id"]
            )
        )
        existing_item = await session.get(LibraryItem, existing_item_id)
        initial_item = (
            await catalogue_service.view(session, existing_item)
            if existing_item is not None
            else None
        )
        return {
            "job_id": str(existing_job.job_id),
            "status": existing_job.status,
            "filename": filename,
            "library_item_id": str(existing_item_id),
            "initial_item": initial_item,
            "reused": True,
        }

    clean_filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    title = PurePosixPath(clean_filename).stem.strip() or "Untitled PDF"
    initialized = await limited_item_service.initialize(
        session,
        actor=actor,
        library_id=library_id,
        metadata={
            "title": title,
            "authors": [],
            "provenance": {"source": "pdf_filename", "filename": clean_filename},
        },
        doi=None,
        collection_ids=[collection_id] if collection_id is not None else [],
    )
    await artifact_service.specify_for_item(
        session,
        library_id=library_id,
        library_item_id=initialized.item.library_item_id,
        artifact_key="pdf",
        artifact_type="SOURCE_PDF",
        blob_id=blob.blob_id,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
        original_filename=clean_filename,
        provenance={"source": "user_pdf_import", "blob_sha256": blob.sha256},
    )
    await artifact_service.set_canonical_if_missing(
        session,
        canonical_paper_id=initialized.item.canonical_paper_id,
        artifact_key="pdf",
        artifact_type="SOURCE_PDF",
        blob_id=blob.blob_id,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
        original_filename=clean_filename,
        provenance={
            "source": "user_pdf_import",
            "blob_sha256": blob.sha256,
            "verification_status": "UNVERIFIED",
            "promoted_from_library_item_id": str(initialized.item.library_item_id),
        },
        verification_status="UNVERIFIED",
    )
    job = await job_service.enqueue(
        session,
        actor,
        library_id,
        job_type=PDF_IMPORT_JOB,
        payload={
            "blob_id": str(blob.blob_id),
            "library_item_id": str(initialized.item.library_item_id),
            "filename": clean_filename,
        },
        idempotency_key=idempotency_key,
        progress_total=3,
        max_attempts=2,
    )
    record_audit_event(
        session,
        "library.pdf_import_requested",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={
            "job_id": str(job.job_id),
            "library_item_id": str(initialized.item.library_item_id),
            "blob_id": str(blob.blob_id),
        },
    )
    initial_item = await catalogue_service.view(
        session,
        initialized.item,
        collection_ids=[collection_id] if collection_id is not None else [],
        tag_ids=[],
    )
    await session.commit()
    return {
        "job_id": str(job.job_id),
        "status": job.status,
        "filename": clean_filename,
        "library_item_id": str(initialized.item.library_item_id),
        "initial_item": initial_item,
        "reused": False,
    }


@router.post("/{library_id}/imports/citations", status_code=202)
async def import_citations(
    library_id: uuid.UUID,
    request: Request,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    filename: str = Query(min_length=1, max_length=1024),
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    data = await request.body()
    limit = get_settings().citation_import_max_bytes
    if not data:
        raise HTTPException(status_code=422, detail="Citation file is empty")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="Citation file exceeds the upload limit")
    lowered = filename.casefold()
    media_type = (
        "application/x-bibtex"
        if lowered.endswith(".bib")
        else "application/x-research-info-systems"
        if lowered.endswith(".ris")
        else "application/vnd.citationstyles.csl+json"
        if lowered.endswith(".json")
        else "application/octet-stream"
    )
    storage = get_object_storage()
    await storage.ensure_bucket()
    blob = await blob_service.store_bytes(
        session,
        storage,
        data=data,
        media_type=media_type,
        actor_principal_id=actor.principal_id,
    )
    job = await job_service.enqueue(
        session,
        actor,
        library_id,
        job_type=CITATION_IMPORT_JOB,
        payload={"blob_id": str(blob.blob_id), "filename": filename},
        idempotency_key=f"citation-blob:{blob.blob_id}",
        progress_total=1,
        max_attempts=2,
    )
    await session.commit()
    return {
        "job_id": str(job.job_id),
        "status": job.status,
        "filename": filename,
    }


@router.post("/{library_id}/items/metadata-refresh", status_code=202)
async def refresh_metadata(
    library_id: uuid.UUID,
    body: MetadataRefreshBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    try:
        jobs = await metadata_refresh_service.enqueue_batch(
            session,
            actor,
            library_id,
            library_item_ids=body.library_item_ids,
            request_id=body.request_id,
            refresh_mode=body.refresh_mode,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {
        "request_id": str(body.request_id),
        "jobs": [
            {
                "job_id": str(job.job_id),
                "library_item_id": job.payload["library_item_id"],
                "status": job.status,
            }
            for job in jobs
        ],
    }


@router.get("/{library_id}/jobs/{job_id}")
async def get_job(
    library_id: uuid.UUID,
    job_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
) -> dict[str, object]:
    await membership_for(session, actor=actor, library_id=library_id)
    job = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.library_id == library_id,
            BackgroundJob.job_id == job_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": str(job.job_id),
        "job_type": job.job_type,
        "status": job.status,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "progress_message": job.progress_message,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "result": job.result,
        "error": job.error,
    }
