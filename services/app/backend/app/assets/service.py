from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Artifact, Blob, CanonicalPaper, ItemArtifactOverride, LibraryItem


@dataclass(frozen=True, slots=True)
class EffectiveArtifact:
    artifact_key: str
    artifact_type: str
    blob_id: uuid.UUID
    media_type: str
    original_filename: str | None
    source_fingerprint: str | None
    verification_status: str | None
    provenance: dict[str, Any]
    origin: Literal["CANONICAL", "OVERRIDE"]
    revision: int


class ArtifactService:
    """Commands and reads for the single-current-value Artifact contract."""

    async def set_canonical(
        self,
        session: AsyncSession,
        *,
        canonical_paper_id: uuid.UUID,
        artifact_key: str,
        artifact_type: str,
        blob_id: uuid.UUID,
        media_type: str,
        actor_principal_id: uuid.UUID | None,
        original_filename: str | None = None,
        provenance: dict[str, Any] | None = None,
        source_fingerprint: str | None = None,
        verification_status: str | None = None,
    ) -> Artifact:
        paper = await session.scalar(
            select(CanonicalPaper)
            .where(CanonicalPaper.canonical_paper_id == canonical_paper_id)
            .with_for_update()
        )
        if paper is None:
            raise LookupError("Canonical Paper not found")
        blob = await session.get(Blob, blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise LookupError("Available Blob not found")
        clean_provenance = provenance or {}
        clean_verification = self._verification_status(
            artifact_type, verification_status, clean_provenance
        )
        artifact = await session.scalar(
            select(Artifact)
            .where(
                Artifact.canonical_paper_id == canonical_paper_id,
                Artifact.artifact_key == artifact_key,
            )
            .with_for_update()
        )
        if artifact is None:
            artifact = Artifact(
                canonical_paper_id=canonical_paper_id,
                artifact_key=artifact_key,
                artifact_type=artifact_type,
                blob_id=blob_id,
                media_type=media_type,
                original_filename=original_filename,
                provenance=clean_provenance,
                source_fingerprint=source_fingerprint,
                verification_status=clean_verification,
                status="ACTIVE",
                revision=1,
                updated_by=actor_principal_id,
            )
            session.add(artifact)
        else:
            artifact.artifact_type = artifact_type
            artifact.blob_id = blob_id
            artifact.media_type = media_type
            artifact.original_filename = original_filename
            artifact.provenance = clean_provenance
            artifact.source_fingerprint = source_fingerprint
            artifact.verification_status = clean_verification
            artifact.status = "ACTIVE"
            artifact.revision += 1
            artifact.updated_by = actor_principal_id
        if artifact_type == "SOURCE_PDF":
            dependents = list(
                await session.scalars(
                    select(Artifact).where(
                        Artifact.canonical_paper_id == canonical_paper_id,
                        Artifact.artifact_type.in_(("EXTRACTED_TEXT", "PIPELINE_DOCUMENT")),
                        Artifact.status == "ACTIVE",
                        Artifact.source_fingerprint.is_not(None),
                        Artifact.source_fingerprint != blob.sha256,
                    )
                )
            )
            for dependent in dependents:
                dependent.status = "STALE"
                dependent.revision += 1
                dependent.updated_by = actor_principal_id
        await session.flush()
        return artifact

    async def set_canonical_if_missing(
        self,
        session: AsyncSession,
        *,
        canonical_paper_id: uuid.UUID,
        artifact_key: str,
        artifact_type: str,
        blob_id: uuid.UUID,
        media_type: str,
        actor_principal_id: uuid.UUID | None,
        original_filename: str | None = None,
        provenance: dict[str, Any] | None = None,
        source_fingerprint: str | None = None,
        verification_status: str | None = None,
    ) -> tuple[Artifact, bool]:
        """Create the first canonical value without replacing an existing truth."""

        paper = await session.scalar(
            select(CanonicalPaper)
            .where(CanonicalPaper.canonical_paper_id == canonical_paper_id)
            .with_for_update()
        )
        if paper is None:
            raise LookupError("Canonical Paper not found")
        existing = await session.scalar(
            select(Artifact).where(
                Artifact.canonical_paper_id == canonical_paper_id,
                Artifact.artifact_key == artifact_key,
            )
        )
        if existing is not None:
            return existing, False
        blob = await session.get(Blob, blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise LookupError("Available Blob not found")
        clean_provenance = provenance or {}
        artifact = Artifact(
            canonical_paper_id=canonical_paper_id,
            artifact_key=artifact_key,
            artifact_type=artifact_type,
            blob_id=blob_id,
            media_type=media_type,
            original_filename=original_filename,
            provenance=clean_provenance,
            source_fingerprint=source_fingerprint,
            verification_status=self._verification_status(
                artifact_type, verification_status, clean_provenance
            ),
            status="ACTIVE",
            revision=1,
            updated_by=actor_principal_id,
        )
        session.add(artifact)
        await session.flush()
        return artifact, True

    @staticmethod
    def _verification_status(
        artifact_type: str,
        value: str | None,
        provenance: dict[str, Any],
    ) -> str | None:
        if artifact_type != "SOURCE_PDF":
            if value is not None:
                raise ValueError("Only SOURCE_PDF Artifacts have a verification status")
            return None
        candidate = value or provenance.get("verification_status") or "UNVERIFIED"
        clean = str(candidate).strip().upper()
        if clean not in {"UNVERIFIED", "VERIFIED"}:
            raise ValueError("verification_status must be UNVERIFIED or VERIFIED")
        return clean

    async def specify_for_item(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        artifact_key: str,
        artifact_type: str,
        blob_id: uuid.UUID,
        media_type: str,
        actor_principal_id: uuid.UUID,
        original_filename: str | None = None,
        provenance: dict[str, Any] | None = None,
        source_fingerprint: str | None = None,
    ) -> ItemArtifactOverride:
        item = await session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.library_id == library_id,
                LibraryItem.library_item_id == library_item_id,
            )
            .with_for_update()
        )
        if item is None:
            raise LookupError("Library Item not found")

        override = await session.scalar(
            select(ItemArtifactOverride)
            .where(
                ItemArtifactOverride.library_id == library_id,
                ItemArtifactOverride.library_item_id == library_item_id,
                ItemArtifactOverride.artifact_key == artifact_key,
            )
            .with_for_update()
        )
        if override is None:
            override = ItemArtifactOverride(
                library_id=library_id,
                library_item_id=library_item_id,
                canonical_paper_id=item.canonical_paper_id,
                artifact_key=artifact_key,
                artifact_type=artifact_type,
                blob_id=blob_id,
                media_type=media_type,
                original_filename=original_filename,
                provenance=provenance or {},
                source_fingerprint=source_fingerprint,
                revision=1,
                specified_by=actor_principal_id,
            )
            session.add(override)
        else:
            override.artifact_type = artifact_type
            override.blob_id = blob_id
            override.media_type = media_type
            override.original_filename = original_filename
            override.provenance = provenance or {}
            override.source_fingerprint = source_fingerprint
            override.revision += 1
            override.specified_by = actor_principal_id
        await session.flush()
        return override

    async def cancel_for_item(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        artifact_key: str,
    ) -> bool:
        override = await session.scalar(
            select(ItemArtifactOverride).where(
                ItemArtifactOverride.library_id == library_id,
                ItemArtifactOverride.library_item_id == library_item_id,
                ItemArtifactOverride.artifact_key == artifact_key,
            )
        )
        if override is None:
            return False
        await session.delete(override)
        await session.flush()
        return True

    async def resolve(
        self,
        session: AsyncSession,
        *,
        library_id: uuid.UUID,
        library_item_id: uuid.UUID,
        artifact_key: str,
    ) -> EffectiveArtifact | None:
        item = await session.scalar(
            select(LibraryItem).where(
                LibraryItem.library_id == library_id,
                LibraryItem.library_item_id == library_item_id,
            )
        )
        if item is None:
            return None

        override = await session.scalar(
            select(ItemArtifactOverride).where(
                ItemArtifactOverride.library_id == library_id,
                ItemArtifactOverride.library_item_id == library_item_id,
                ItemArtifactOverride.artifact_key == artifact_key,
            )
        )
        if override is not None:
            return EffectiveArtifact(
                artifact_key=override.artifact_key,
                artifact_type=override.artifact_type,
                blob_id=override.blob_id,
                media_type=override.media_type,
                original_filename=override.original_filename,
                source_fingerprint=override.source_fingerprint,
                verification_status=None,
                provenance=dict(override.provenance),
                origin="OVERRIDE",
                revision=override.revision,
            )

        artifact = await session.scalar(
            select(Artifact).where(
                Artifact.canonical_paper_id == item.canonical_paper_id,
                Artifact.artifact_key == artifact_key,
                Artifact.status == "ACTIVE",
            )
        )
        if artifact is None:
            return None
        return EffectiveArtifact(
            artifact_key=artifact.artifact_key,
            artifact_type=artifact.artifact_type,
            blob_id=artifact.blob_id,
            media_type=artifact.media_type,
            original_filename=artifact.original_filename,
            source_fingerprint=artifact.source_fingerprint,
            verification_status=artifact.verification_status,
            provenance=dict(artifact.provenance),
            origin="CANONICAL",
            revision=artifact.revision,
        )


artifact_service = ArtifactService()
