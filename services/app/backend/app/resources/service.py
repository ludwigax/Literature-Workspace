from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.service import EffectiveArtifact, artifact_service
from backend.app.assets.storage import ObjectStorage
from backend.app.models import (
    Artifact,
    Asset,
    Blob,
    LibraryItem,
    PipelineDocument,
)


class ResourceService:
    async def catalogue(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
    ) -> dict[str, object]:
        item = await self.require_item(session, library_id, library_item_id)
        artifacts = list(
            await session.scalars(
                select(Artifact)
                .where(Artifact.canonical_paper_id == item.canonical_paper_id)
                .order_by(Artifact.artifact_type, Artifact.artifact_key)
            )
        )
        assets = list(
            await session.scalars(
                select(Asset)
                .where(
                    Asset.library_id == library_id,
                    Asset.library_item_id == library_item_id,
                    Asset.status == "ACTIVE",
                )
                .order_by(Asset.created_at, Asset.asset_id)
            )
        )
        effective_pdf = await artifact_service.resolve(
            session,
            library_id=library_id,
            library_item_id=library_item_id,
            artifact_key="pdf",
        )
        blob_ids = {value.blob_id for value in artifacts}
        blob_ids.update(value.blob_id for value in assets)
        if effective_pdf is not None:
            blob_ids.add(effective_pdf.blob_id)
        blobs = (
            {
                value.blob_id: value
                for value in await session.scalars(select(Blob).where(Blob.blob_id.in_(blob_ids)))
            }
            if blob_ids
            else {}
        )

        documents = [
            self.artifact_view(value, blobs.get(value.blob_id), origin="CANONICAL")
            for value in artifacts
            if value.artifact_type == "PIPELINE_DOCUMENT"
        ]
        canonical_attachments = [
            self.artifact_view(value, blobs.get(value.blob_id), origin="CANONICAL")
            for value in artifacts
            if value.artifact_type not in {"SOURCE_PDF", "PIPELINE_DOCUMENT"}
        ]
        return {
            "library_item_id": str(item.library_item_id),
            "canonical_paper_id": str(item.canonical_paper_id),
            "primary_pdf": (
                self.effective_view(effective_pdf, blobs.get(effective_pdf.blob_id))
                if effective_pdf is not None
                else None
            ),
            "documents": documents,
            "canonical_attachments": canonical_attachments,
            "assets": [self.asset_view(value, blobs.get(value.blob_id)) for value in assets],
        }

    async def artifact_blob(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        artifact_key: str,
    ) -> tuple[Blob, str | None]:
        item = await self.require_item(session, library_id, library_item_id)
        value = await artifact_service.resolve(
            session,
            library_id=library_id,
            library_item_id=library_item_id,
            artifact_key=artifact_key,
        )
        if value is None:
            stale = await session.scalar(
                select(Artifact).where(
                    Artifact.canonical_paper_id == item.canonical_paper_id,
                    Artifact.artifact_key == artifact_key,
                    Artifact.artifact_type != "SOURCE_PDF",
                    Artifact.status == "STALE",
                )
            )
            if stale is None:
                raise HTTPException(status_code=404, detail="Artifact resource not found")
            if (
                stale.artifact_type == "PIPELINE_DOCUMENT"
                and stale.provenance.get("projection_kind") == "DOCUMENT_DATABASE_CURRENT"
            ):
                return await self.projected_document_blob(
                    session,
                    canonical_paper_id=item.canonical_paper_id,
                    provenance=stale.provenance,
                    filename=stale.original_filename,
                )
            return await self.require_blob(session, stale.blob_id), stale.original_filename
        if (
            value.artifact_type == "PIPELINE_DOCUMENT"
            and value.provenance.get("projection_kind") == "DOCUMENT_DATABASE_CURRENT"
        ):
            return await self.projected_document_blob(
                session,
                canonical_paper_id=item.canonical_paper_id,
                provenance=value.provenance,
                filename=value.original_filename,
            )
        blob = await self.require_blob(session, value.blob_id)
        return blob, value.original_filename

    async def document_blob(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> tuple[Blob, str]:
        item = await self.require_item(session, library_id, library_item_id)
        document = await session.get(PipelineDocument, document_id)
        if document is None or document.canonical_paper_id != item.canonical_paper_id:
            raise HTTPException(status_code=404, detail="Document resource not found")
        blob = await self.require_blob(session, document.content_blob_id)
        return blob, document.display_title

    async def projected_document_blob(
        self,
        session: AsyncSession,
        *,
        canonical_paper_id: uuid.UUID,
        provenance: dict[str, object],
        filename: str | None,
    ) -> tuple[Blob, str | None]:
        raw_document_id = provenance.get("document_id")
        if not isinstance(raw_document_id, str):
            raise HTTPException(status_code=404, detail="Document projection is unavailable")
        try:
            document_id = uuid.UUID(raw_document_id)
        except ValueError as error:
            raise HTTPException(
                status_code=404, detail="Document projection is unavailable"
            ) from error
        document = await session.get(PipelineDocument, document_id)
        if document is None or document.canonical_paper_id != canonical_paper_id:
            raise HTTPException(status_code=404, detail="Document projection is unavailable")
        return await self.require_blob(session, document.content_blob_id), filename

    async def asset_blob(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        asset_id: uuid.UUID,
    ) -> tuple[Blob, str]:
        await self.require_item(session, library_id, library_item_id)
        asset = await self.require_asset(session, library_id, library_item_id, asset_id)
        return await self.require_blob(session, asset.blob_id), asset.display_name

    async def signed_url(self, storage: ObjectStorage, blob: Blob) -> str:
        return await storage.presigned_get(blob.storage_key, timedelta(minutes=5))

    @staticmethod
    async def require_item(
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
            raise HTTPException(status_code=404, detail="Library Item resource not found")
        return item

    @staticmethod
    async def require_asset(
        session: AsyncSession,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        asset_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Asset:
        statement = select(Asset).where(
            Asset.asset_id == asset_id,
            Asset.library_id == library_id,
            Asset.library_item_id == library_item_id,
            Asset.status == "ACTIVE",
        )
        if lock:
            statement = statement.with_for_update()
        asset = await session.scalar(statement)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset resource not found")
        return asset

    @staticmethod
    async def require_blob(session: AsyncSession, blob_id: uuid.UUID) -> Blob:
        blob = await session.get(Blob, blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise HTTPException(status_code=404, detail="Resource content is unavailable")
        return blob

    @staticmethod
    def effective_view(value: EffectiveArtifact, blob: Blob | None) -> dict[str, object]:
        result: dict[str, object] = {
            "resource_kind": "ARTIFACT",
            "artifact_key": value.artifact_key,
            "artifact_type": value.artifact_type,
            "origin": value.origin,
            "filename": value.original_filename,
            "media_type": value.media_type,
            "byte_size": blob.byte_size if blob is not None else None,
            "revision": value.revision,
            "verification_status": value.verification_status,
            "status": "ACTIVE" if blob is not None and blob.status == "AVAILABLE" else "MISSING",
        }
        ResourceService.add_document_projection(result, value.provenance)
        return result

    @staticmethod
    def artifact_view(value: Artifact, blob: Blob | None, *, origin: str) -> dict[str, object]:
        result: dict[str, object] = {
            "resource_kind": "ARTIFACT",
            "artifact_key": value.artifact_key,
            "artifact_type": value.artifact_type,
            "origin": origin,
            "filename": value.original_filename,
            "media_type": value.media_type,
            "byte_size": blob.byte_size if blob is not None else None,
            "revision": value.revision,
            "verification_status": value.verification_status,
            "status": (
                value.status if blob is not None and blob.status == "AVAILABLE" else "MISSING"
            ),
        }
        ResourceService.add_document_projection(result, value.provenance)
        return result

    @staticmethod
    def add_document_projection(result: dict[str, object], provenance: dict[str, object]) -> None:
        if provenance.get("projection_kind") != "DOCUMENT_DATABASE_CURRENT":
            return
        for key in (
            "document_id",
            "document_database_id",
            "pipeline_id",
            "pipeline_version_id",
        ):
            value = provenance.get(key)
            if isinstance(value, str):
                result[key] = value

    @staticmethod
    def asset_view(value: Asset, blob: Blob | None) -> dict[str, object]:
        return {
            "resource_kind": "ASSET",
            "asset_id": str(value.asset_id),
            "filename": value.display_name,
            "media_type": value.media_type,
            "byte_size": blob.byte_size if blob is not None else None,
            "revision": value.revision,
            "status": "ACTIVE" if blob is not None and blob.status == "AVAILABLE" else "MISSING",
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
        }


resource_service = ResourceService()
