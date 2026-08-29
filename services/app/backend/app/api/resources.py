from __future__ import annotations

import uuid
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.responses import RedirectResponse

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
from backend.app.config import get_settings
from backend.app.models import Asset, ItemArtifactOverride
from backend.app.resources.service import resource_service

router = APIRouter(prefix="/libraries", tags=["resources"])


class RenameAssetBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=1024)
    expected_revision: int = Field(ge=1)


@router.get("/{library_id}/items/{library_item_id}/resources")
async def list_item_resources(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
) -> dict[str, object]:
    await membership_for(session, actor=actor, library_id=library_id)
    return await resource_service.catalogue(
        session,
        library_id=library_id,
        library_item_id=library_item_id,
    )


@router.put("/{library_id}/items/{library_item_id}/resources/primary-pdf")
async def upload_primary_pdf_override(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    request: Request,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    filename: str = Query(min_length=1, max_length=1024),
    expected_revision: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    item = await resource_service.require_item(session, library_id, library_item_id, lock=True)
    if item.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Trashed items cannot be modified")
    current = await session.scalar(
        select(ItemArtifactOverride)
        .where(
            ItemArtifactOverride.library_id == library_id,
            ItemArtifactOverride.library_item_id == library_item_id,
            ItemArtifactOverride.artifact_key == "pdf",
        )
        .with_for_update()
    )
    if current is not None and expected_revision != current.revision:
        raise HTTPException(
            status_code=409,
            detail="expected_revision is required and must match the current PDF override",
        )
    if current is None and expected_revision is not None:
        raise HTTPException(status_code=409, detail="The PDF override no longer exists")
    data = await request.body()
    limit = get_settings().pdf_import_max_bytes
    if not data:
        raise HTTPException(status_code=422, detail="PDF is empty")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="PDF exceeds the upload limit")
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(status_code=422, detail="Uploaded file is not a PDF")
    clean_filename = PurePosixPath(filename.replace("\\", "/")).name
    storage = get_object_storage()
    await storage.ensure_bucket()
    blob = await blob_service.store_bytes(
        session,
        storage,
        data=data,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
    )
    override = await artifact_service.specify_for_item(
        session,
        library_id=library_id,
        library_item_id=library_item_id,
        artifact_key="pdf",
        artifact_type="SOURCE_PDF",
        blob_id=blob.blob_id,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
        original_filename=clean_filename,
        provenance={"source": "user_pdf_override", "blob_sha256": blob.sha256},
    )
    _canonical_artifact, promoted = await artifact_service.set_canonical_if_missing(
        session,
        canonical_paper_id=item.canonical_paper_id,
        artifact_key="pdf",
        artifact_type="SOURCE_PDF",
        blob_id=blob.blob_id,
        media_type="application/pdf",
        actor_principal_id=actor.principal_id,
        original_filename=clean_filename,
        provenance={
            "source": "user_pdf_override",
            "blob_sha256": blob.sha256,
            "verification_status": "UNVERIFIED",
            "promoted_from_library_item_id": str(item.library_item_id),
        },
        verification_status="UNVERIFIED",
    )
    record_audit_event(
        session,
        "library.primary_pdf_override_set",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={
            "library_item_id": str(library_item_id),
            "override_revision": override.revision,
            "canonical_promoted": promoted,
        },
    )
    resources = await resource_service.catalogue(
        session, library_id=library_id, library_item_id=library_item_id
    )
    await session.commit()
    return {
        "primary_pdf": resources["primary_pdf"],
        "canonical_promoted": promoted,
    }


@router.delete("/{library_id}/items/{library_item_id}/resources/primary-pdf")
async def cancel_primary_pdf_override(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    expected_revision: int = Query(ge=1),
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    await resource_service.require_item(session, library_id, library_item_id, lock=True)
    current = await session.scalar(
        select(ItemArtifactOverride)
        .where(
            ItemArtifactOverride.library_id == library_id,
            ItemArtifactOverride.library_item_id == library_item_id,
            ItemArtifactOverride.artifact_key == "pdf",
        )
        .with_for_update()
    )
    if current is None:
        raise HTTPException(status_code=404, detail="PDF override not found")
    if current.revision != expected_revision:
        raise HTTPException(status_code=409, detail="PDF override revision conflict")
    await session.delete(current)
    await session.flush()
    record_audit_event(
        session,
        "library.primary_pdf_override_cancelled",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={"library_item_id": str(library_item_id)},
    )
    resources = await resource_service.catalogue(
        session, library_id=library_id, library_item_id=library_item_id
    )
    await session.commit()
    return {"primary_pdf": resources["primary_pdf"]}


@router.post("/{library_id}/items/{library_item_id}/resources/assets", status_code=201)
async def upload_asset(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
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
    item = await resource_service.require_item(session, library_id, library_item_id)
    if item.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Trashed items cannot be modified")
    data = await request.body()
    limit = get_settings().asset_upload_max_bytes
    if not data:
        raise HTTPException(status_code=422, detail="Attachment is empty")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="Attachment exceeds the upload limit")
    clean_filename = PurePosixPath(filename.replace("\\", "/")).name
    media_type = request.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
    storage = get_object_storage()
    await storage.ensure_bucket()
    blob = await blob_service.store_bytes(
        session,
        storage,
        data=data,
        media_type=media_type,
        actor_principal_id=actor.principal_id,
    )
    asset = Asset(
        library_id=library_id,
        library_item_id=library_item_id,
        blob_id=blob.blob_id,
        display_name=clean_filename,
        media_type=media_type,
        status="ACTIVE",
        provenance={"source": "user_attachment", "blob_sha256": blob.sha256},
        revision=1,
        created_by=actor.principal_id,
    )
    session.add(asset)
    await session.flush()
    record_audit_event(
        session,
        "library.asset_uploaded",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={"library_item_id": str(library_item_id), "asset_id": str(asset.asset_id)},
    )
    await session.flush()
    await session.refresh(asset)
    result = resource_service.asset_view(asset, blob)
    await session.commit()
    return result


@router.patch("/{library_id}/items/{library_item_id}/resources/assets/{asset_id}")
async def rename_asset(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: RenameAssetBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    asset = await resource_service.require_asset(
        session, library_id, library_item_id, asset_id, lock=True
    )
    if asset.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Asset revision conflict")
    asset.display_name = PurePosixPath(body.display_name.replace("\\", "/")).name
    asset.revision += 1
    blob = await resource_service.require_blob(session, asset.blob_id)
    record_audit_event(
        session,
        "library.asset_renamed",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={"library_item_id": str(library_item_id), "asset_id": str(asset.asset_id)},
    )
    await session.flush()
    await session.refresh(asset)
    result = resource_service.asset_view(asset, blob)
    await session.commit()
    return result


@router.delete("/{library_id}/items/{library_item_id}/resources/assets/{asset_id}", status_code=204)
async def delete_asset(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
    expected_revision: int = Query(ge=1),
) -> None:
    await membership_for(
        session,
        actor=actor,
        library_id=library_id,
        allowed_roles={"OWNER", "EDITOR"},
    )
    asset = await resource_service.require_asset(
        session, library_id, library_item_id, asset_id, lock=True
    )
    if asset.revision != expected_revision:
        raise HTTPException(status_code=409, detail="Asset revision conflict")
    asset.status = "DELETED"
    asset.revision += 1
    record_audit_event(
        session,
        "library.asset_deleted",
        actor_principal_id=actor.principal_id,
        library_id=library_id,
        details={"library_item_id": str(library_item_id), "asset_id": str(asset.asset_id)},
    )
    await session.commit()


@router.get("/{library_id}/items/{library_item_id}/resources/artifacts/{artifact_key}/content")
async def open_artifact_content(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    artifact_key: str,
    session: Database,
    actor: CurrentActor,
) -> RedirectResponse:
    await membership_for(session, actor=actor, library_id=library_id)
    blob, _ = await resource_service.artifact_blob(
        session,
        library_id=library_id,
        library_item_id=library_item_id,
        artifact_key=artifact_key,
    )
    url = await resource_service.signed_url(get_object_storage(), blob)
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


@router.get("/{library_id}/items/{library_item_id}/resources/assets/{asset_id}/content")
async def open_asset_content(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
) -> RedirectResponse:
    await membership_for(session, actor=actor, library_id=library_id)
    blob, _ = await resource_service.asset_blob(
        session,
        library_id=library_id,
        library_item_id=library_item_id,
        asset_id=asset_id,
    )
    url = await resource_service.signed_url(get_object_storage(), blob)
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


@router.get("/{library_id}/items/{library_item_id}/resources/documents/{document_id}/content")
async def open_document_content(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    document_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
) -> RedirectResponse:
    await membership_for(session, actor=actor, library_id=library_id)
    blob, _ = await resource_service.document_blob(
        session,
        library_id=library_id,
        library_item_id=library_item_id,
        document_id=document_id,
    )
    url = await resource_service.signed_url(get_object_storage(), blob)
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})
