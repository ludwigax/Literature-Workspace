from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import ObjectStorage
from backend.app.models import (
    Artifact,
    Blob,
    DocumentBuildRun,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabasePaperScope,
    DocumentDatabaseRelease,
    DocumentPipeline,
    DocumentPipelineVersion,
    DocumentReleaseEntry,
    DocumentReleaseIndex,
    PipelineDocument,
)

from .executor import PipelineExecutor
from .indexing import document_index_service
from .reconcile import ManifestEntry, plan_reconciliation
from .splitters import sanitize_external_text, split_output


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def render_messages(
    version: DocumentPipelineVersion, *, source_text: str, user_note: str = ""
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if version.system_prompt.strip():
        messages.append({"role": "system", "content": version.system_prompt.strip()})
    parts = [
        version.user_prompt.strip(),
        f'<source type="canonical_pdf_text">\n{source_text}\n</source>',
    ]
    if user_note.strip():
        parts.append(f"<user_note>\n{user_note.strip()}\n</user_note>")
    messages.append({"role": "user", "content": "\n\n".join(part for part in parts if part)})
    return messages


class DocumentDomainService:
    async def create_pipeline(
        self,
        session: AsyncSession,
        *,
        name: str,
        description: str = "",
        created_by: uuid.UUID | None = None,
    ) -> DocumentPipeline:
        pipeline = DocumentPipeline(
            name=name.strip(),
            description=description.strip(),
            status="ACTIVE",
            created_by=created_by,
        )
        if not pipeline.name:
            raise ValueError("Pipeline name is required")
        session.add(pipeline)
        await session.flush()
        return pipeline

    async def add_pipeline_version(
        self,
        session: AsyncSession,
        pipeline_id: uuid.UUID,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        splitter_type: str,
        splitter_config: dict[str, Any] | None = None,
        model_config: dict[str, Any] | None = None,
        input_config: dict[str, Any] | None = None,
        created_by: uuid.UUID | None = None,
        activate: bool = True,
    ) -> DocumentPipelineVersion:
        pipeline = await session.get(DocumentPipeline, pipeline_id, with_for_update=True)
        if pipeline is None:
            raise LookupError("Pipeline not found")
        kind = splitter_type.strip().upper()
        if kind not in {"WHOLE", "JSON", "PARAGRAPH", "MARKDOWN", "ADVANCED"}:
            raise ValueError("Unsupported splitter_type")
        clean_input_config = input_config or {
            "source": "canonical_pdf_text",
            "execution_mode": "LLM",
        }
        execution_mode = str(clean_input_config.get("execution_mode") or "LLM").upper()
        if execution_mode not in {"DIRECT_TEXT", "LLM"}:
            raise ValueError("input_config.execution_mode must be DIRECT_TEXT or LLM")
        clean_input_config = {**clean_input_config, "execution_mode": execution_mode}
        clean_user_prompt = user_prompt.strip()
        clean_model = model.strip()
        if execution_mode == "LLM" and (not clean_user_prompt or not clean_model):
            raise ValueError("LLM Pipelines require user_prompt and model")
        if execution_mode == "DIRECT_TEXT":
            clean_model = clean_model or "builtin:direct-text"
        config = {
            "system_prompt": system_prompt.strip(),
            "user_prompt": clean_user_prompt,
            "model": clean_model,
            "model_config": model_config or {},
            "input_config": clean_input_config,
            "splitter_type": kind,
            "splitter_config": splitter_config or {},
        }
        existing = await session.scalar(
            select(DocumentPipelineVersion).where(
                DocumentPipelineVersion.pipeline_id == pipeline_id,
                DocumentPipelineVersion.config_hash == stable_hash(config),
            )
        )
        if existing is not None:
            previous_active_id = pipeline.active_version_id
            if activate:
                pipeline.active_version_id = existing.pipeline_version_id
                if previous_active_id != existing.pipeline_version_id:
                    await session.execute(
                        update(DocumentBuildRun)
                        .where(
                            DocumentBuildRun.database_id.in_(
                                select(DocumentDatabase.database_id).where(
                                    DocumentDatabase.pipeline_id == pipeline_id
                                )
                            ),
                            DocumentBuildRun.status == "RUNNING",
                        )
                        .values(reconcile_requested=True)
                    )
            return existing
        next_version = (
            int(
                await session.scalar(
                    select(func.coalesce(func.max(DocumentPipelineVersion.version), 0)).where(
                        DocumentPipelineVersion.pipeline_id == pipeline_id
                    )
                )
                or 0
            )
            + 1
        )
        version = DocumentPipelineVersion(
            pipeline_id=pipeline_id,
            version=next_version,
            config_hash=stable_hash(config),
            created_by=created_by,
            **config,
        )
        session.add(version)
        await session.flush()
        if activate:
            pipeline.active_version_id = version.pipeline_version_id
            await session.execute(
                update(DocumentBuildRun)
                .where(
                    DocumentBuildRun.database_id.in_(
                        select(DocumentDatabase.database_id).where(
                            DocumentDatabase.pipeline_id == pipeline_id
                        )
                    ),
                    DocumentBuildRun.status == "RUNNING",
                )
                .values(reconcile_requested=True)
            )
        await session.flush()
        return version

    async def create_database(
        self,
        session: AsyncSession,
        *,
        pipeline_id: uuid.UUID,
        name: str,
        description: str = "",
        range_mode: str = "EXPLICIT",
        embedding_profile: dict[str, Any] | None = None,
        bm25_profile: dict[str, Any] | None = None,
        created_by: uuid.UUID | None = None,
    ) -> DocumentDatabase:
        mode = range_mode.strip().upper()
        if mode not in {"EXPLICIT", "ALL_VERIFIED"}:
            raise ValueError("range_mode must be EXPLICIT or ALL_VERIFIED")
        database = DocumentDatabase(
            pipeline_id=pipeline_id,
            name=name.strip(),
            description=description.strip(),
            range_mode=mode,
            range_revision=1,
            embedding_profile=embedding_profile or {},
            bm25_profile=bm25_profile or {},
            retrieval_status="NOT_CONFIGURED",
            created_by=created_by,
        )
        if not database.name:
            raise ValueError("Document Database name is required")
        session.add(database)
        await session.flush()
        return database

    async def replace_explicit_scope(
        self,
        session: AsyncSession,
        database_id: uuid.UUID,
        paper_ids: set[uuid.UUID],
        *,
        actor_principal_id: uuid.UUID | None = None,
    ) -> bool:
        database = await session.get(DocumentDatabase, database_id, with_for_update=True)
        if database is None:
            raise LookupError("Document Database not found")
        if database.range_mode != "EXPLICIT":
            raise ValueError("Only EXPLICIT ranges have editable Paper membership")
        current = set(
            await session.scalars(
                select(DocumentDatabasePaperScope.canonical_paper_id).where(
                    DocumentDatabasePaperScope.database_id == database_id
                )
            )
        )
        if current == paper_ids:
            return False
        await session.execute(
            text("DELETE FROM document_database_paper_scope WHERE database_id = :database_id"),
            {"database_id": database_id},
        )
        session.add_all(
            DocumentDatabasePaperScope(
                database_id=database_id,
                canonical_paper_id=paper_id,
                added_by=actor_principal_id,
            )
            for paper_id in sorted(paper_ids, key=str)
        )
        database.range_revision += 1
        await session.execute(
            update(DocumentBuildRun)
            .where(
                DocumentBuildRun.database_id == database_id,
                DocumentBuildRun.status == "RUNNING",
            )
            .values(reconcile_requested=True)
        )
        await session.flush()
        return True

    async def set_range_mode(
        self,
        session: AsyncSession,
        database_id: uuid.UUID,
        range_mode: str,
    ) -> tuple[DocumentDatabase, bool]:
        mode = range_mode.strip().upper()
        if mode not in {"EXPLICIT", "ALL_VERIFIED"}:
            raise ValueError("range_mode must be EXPLICIT or ALL_VERIFIED")
        database = await session.get(DocumentDatabase, database_id, with_for_update=True)
        if database is None:
            raise LookupError("Document Database not found")
        if database.range_mode == mode:
            return database, False
        database.range_mode = mode
        database.range_revision += 1
        await session.execute(
            update(DocumentBuildRun)
            .where(
                DocumentBuildRun.database_id == database_id,
                DocumentBuildRun.status == "RUNNING",
            )
            .values(reconcile_requested=True)
        )
        await session.flush()
        return database, True

    async def resolve_scope(
        self,
        session: AsyncSession,
        database: DocumentDatabase,
    ) -> list[uuid.UUID]:
        if database.range_mode == "EXPLICIT":
            statement = (
                select(DocumentDatabasePaperScope.canonical_paper_id)
                .where(DocumentDatabasePaperScope.database_id == database.database_id)
                .order_by(DocumentDatabasePaperScope.canonical_paper_id)
            )
        elif database.range_mode == "ALL_VERIFIED":
            statement = (
                select(Artifact.canonical_paper_id)
                .join(Blob, Blob.blob_id == Artifact.blob_id)
                .where(
                    Artifact.artifact_key == "pdf",
                    Artifact.artifact_type == "SOURCE_PDF",
                    Artifact.status == "ACTIVE",
                    Artifact.verification_status == "VERIFIED",
                    Blob.status == "AVAILABLE",
                )
                .distinct()
                .order_by(Artifact.canonical_paper_id)
            )
        else:
            raise RuntimeError("Document Database has an unsupported range_mode")
        return list(await session.scalars(statement))

    async def set_pdf_verification(
        self,
        session: AsyncSession,
        canonical_paper_id: uuid.UUID,
        verification_status: str,
        *,
        actor_principal_id: uuid.UUID | None,
    ) -> tuple[Artifact, bool]:
        clean = verification_status.strip().upper()
        if clean not in {"UNVERIFIED", "VERIFIED"}:
            raise ValueError("verification_status must be UNVERIFIED or VERIFIED")
        artifact = await session.scalar(
            select(Artifact)
            .where(
                Artifact.canonical_paper_id == canonical_paper_id,
                Artifact.artifact_key == "pdf",
                Artifact.artifact_type == "SOURCE_PDF",
                Artifact.status == "ACTIVE",
            )
            .with_for_update()
        )
        if artifact is None:
            raise LookupError("Active canonical PDF not found")
        blob = await session.get(Blob, artifact.blob_id)
        if clean == "VERIFIED" and (blob is None or blob.status != "AVAILABLE"):
            raise RuntimeError("An unavailable canonical PDF cannot be verified")
        if artifact.verification_status == clean:
            return artifact, False
        artifact.verification_status = clean
        artifact.provenance = {**artifact.provenance, "verification_status": clean}
        artifact.revision += 1
        artifact.updated_by = actor_principal_id
        all_verified_databases = select(DocumentDatabase.database_id).where(
            DocumentDatabase.range_mode == "ALL_VERIFIED"
        )
        await session.execute(
            update(DocumentDatabase)
            .where(DocumentDatabase.range_mode == "ALL_VERIFIED")
            .values(range_revision=DocumentDatabase.range_revision + 1)
        )
        await session.execute(
            update(DocumentBuildRun)
            .where(
                DocumentBuildRun.database_id.in_(all_verified_databases),
                DocumentBuildRun.status == "RUNNING",
            )
            .values(reconcile_requested=True)
        )
        await session.flush()
        return artifact, True

    async def start_reconcile(
        self,
        session: AsyncSession,
        database_id: uuid.UUID,
        *,
        build_mode: str = "UPDATE",
        trigger_reason: str = "MANUAL",
    ) -> DocumentDatabaseRelease | None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"document-database:{database_id}"},
        )
        database = await session.get(DocumentDatabase, database_id, with_for_update=True)
        if database is None:
            raise LookupError("Document Database not found")
        if database.building_release_id is not None:
            raise RuntimeError("A BUILDING release already exists; reconcile must be coalesced")
        pipeline = await session.get(DocumentPipeline, database.pipeline_id)
        if pipeline is None or pipeline.active_version_id is None:
            raise RuntimeError("Document Database Pipeline has no active version")
        version = await session.get(DocumentPipelineVersion, pipeline.active_version_id)
        if version is None:
            raise RuntimeError("Active Pipeline version is missing")
        paper_ids = await self.resolve_scope(session, database)
        desired = await self._desired_manifest(session, paper_ids)
        splitter_hash = stable_hash(
            {"type": version.splitter_type, "config": version.splitter_config}
        )
        current_by_paper, current_manifest = await self._current_manifest(
            session,
            database,
            pipeline_version_id=version.pipeline_version_id,
            splitter_config_hash=splitter_hash,
        )
        plan = plan_reconciliation(
            desired=desired,
            current_by_paper=current_by_paper,
            pipeline_version_id=version.pipeline_version_id,
            splitter_config_hash=splitter_hash,
            build_mode=build_mode,
            current_manifest_hash=current_manifest,
        )
        if plan.outcome == "NO_CHANGE":
            return None
        next_number = (
            int(
                await session.scalar(
                    select(
                        func.coalesce(func.max(DocumentDatabaseRelease.release_number), 0)
                    ).where(DocumentDatabaseRelease.database_id == database_id)
                )
                or 0
            )
            + 1
        )
        profiles_configured = bool(database.embedding_profile or database.bm25_profile)
        release = DocumentDatabaseRelease(
            database_id=database_id,
            release_number=next_number,
            pipeline_version_id=version.pipeline_version_id,
            range_revision=database.range_revision,
            target_manifest_hash=plan.target_manifest_hash,
            build_mode=build_mode.strip().upper(),
            trigger_reason=trigger_reason.strip().upper(),
            status="BUILDING",
            expected_count=len(desired),
            completed_count=len(plan.reuse),
            failed_count=0,
            embedding_profile=dict(database.embedding_profile),
            bm25_profile=dict(database.bm25_profile),
            retrieval_status="PENDING" if profiles_configured else "NOT_CONFIGURED",
        )
        session.add(release)
        await session.flush()
        database.building_release_id = release.release_id
        session.add_all(
            DocumentReleaseEntry(
                release_id=release.release_id,
                canonical_paper_id=entry.canonical_paper_id,
                source_artifact_id=entry.source_artifact_id,
                source_fingerprint=entry.source_fingerprint,
                document_id=entry.reusable_document_id,
                status="REUSED",
            )
            for entry in plan.reuse
        )
        for entry in plan.build:
            missing = entry.source_artifact_id is None
            session.add(
                DocumentReleaseEntry(
                    release_id=release.release_id,
                    canonical_paper_id=entry.canonical_paper_id,
                    source_artifact_id=entry.source_artifact_id,
                    source_fingerprint=entry.source_fingerprint,
                    status="FAILED" if missing else "PENDING",
                    error={"code": "SOURCE_TEXT_MISSING"} if missing else None,
                )
            )
            if missing:
                release.failed_count += 1
        await session.flush()
        return release

    async def materialize_item(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        release_id: uuid.UUID,
        canonical_paper_id: uuid.UUID,
        raw_output: str,
        actor_principal_id: uuid.UUID | None = None,
    ) -> PipelineDocument:
        item = await session.get(
            DocumentReleaseEntry,
            {"release_id": release_id, "canonical_paper_id": canonical_paper_id},
            with_for_update=True,
        )
        if item is None:
            raise LookupError("Release item not found")
        if item.status in {"REUSED", "SUCCEEDED"} and item.document_id is not None:
            document = await session.get(PipelineDocument, item.document_id)
            if document is None:
                raise RuntimeError("Release item references a missing Document")
            return document
        if item.status == "FAILED" and item.source_artifact_id is None:
            raise RuntimeError("Release item has no source PDF text")
        release = await session.get(DocumentDatabaseRelease, release_id, with_for_update=True)
        if release is None or release.status != "BUILDING":
            raise RuntimeError("Release is not BUILDING")
        version = await session.get(DocumentPipelineVersion, release.pipeline_version_id)
        if version is None:
            raise RuntimeError("Pipeline version is missing")
        database = await session.get(DocumentDatabase, release.database_id)
        if database is None:
            raise RuntimeError("Document Database is missing")
        item.status = "RUNNING"
        clean_raw_output = sanitize_external_text(raw_output)
        result = split_output(clean_raw_output, version.splitter_type, version.splitter_config)
        raw_blob = await blob_service.store_bytes(
            session,
            storage,
            data=clean_raw_output.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            actor_principal_id=actor_principal_id,
        )
        content_bytes = result.document_content.encode("utf-8")
        content_blob = await blob_service.store_bytes(
            session,
            storage,
            data=content_bytes,
            media_type="text/plain; charset=utf-8",
            actor_principal_id=actor_principal_id,
        )
        document = PipelineDocument(
            canonical_paper_id=canonical_paper_id,
            pipeline_version_id=release.pipeline_version_id,
            source_artifact_id=item.source_artifact_id,
            source_fingerprint=item.source_fingerprint,
            splitter_config_hash=stable_hash(
                {"type": version.splitter_type, "config": version.splitter_config}
            ),
            content_blob_id=content_blob.blob_id,
            raw_output_blob_id=raw_blob.blob_id,
            display_title=database.name,
            media_type=("application/json" if version.splitter_type == "JSON" else "text/markdown"),
            content_sha256=hashlib.sha256(content_bytes).hexdigest(),
            word_count=len(result.document_content.split()),
            provenance={"release_id": str(release_id)},
        )
        session.add(document)
        await session.flush()
        session.add_all(
            DocumentChunk(
                document_id=document.document_id,
                canonical_paper_id=canonical_paper_id,
                ordinal=ordinal,
                content=chunk.content,
                content_sha256=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                word_count=len(chunk.content.split()),
                facet_1=chunk.facet_1,
                facet_2=chunk.facet_2,
                attributes=chunk.attributes,
            )
            for ordinal, chunk in enumerate(result.chunks)
        )
        item.document_id = document.document_id
        item.status = "SUCCEEDED"
        item.error = None
        release.completed_count += 1
        await session.flush()
        return document

    async def execute_item(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        executor: PipelineExecutor,
        *,
        release_id: uuid.UUID,
        canonical_paper_id: uuid.UUID,
        actor_principal_id: uuid.UUID | None = None,
        user_note: str = "",
        max_source_bytes: int = 100 * 1024 * 1024,
    ) -> PipelineDocument:
        item = await session.get(
            DocumentReleaseEntry,
            {"release_id": release_id, "canonical_paper_id": canonical_paper_id},
        )
        if item is None or item.source_artifact_id is None:
            raise RuntimeError("Release item has no executable source")
        artifact = await session.get(Artifact, item.source_artifact_id)
        if artifact is None:
            raise RuntimeError("Release source Artifact is missing")
        blob = await session.get(Blob, artifact.blob_id)
        if blob is None or blob.status != "AVAILABLE":
            raise RuntimeError("Release source Blob is unavailable")
        release = await session.get(DocumentDatabaseRelease, release_id)
        if release is None:
            raise LookupError("Release not found")
        version = await session.get(DocumentPipelineVersion, release.pipeline_version_id)
        if version is None:
            raise RuntimeError("Pipeline version is missing")
        source_bytes = await storage.read_bytes(blob.storage_key, max_source_bytes)
        source_text = sanitize_external_text(source_bytes.decode("utf-8"))
        execution_mode = str(version.input_config.get("execution_mode") or "LLM").upper()
        if execution_mode == "DIRECT_TEXT":
            raw_output = source_text
        elif execution_mode == "LLM":
            messages = render_messages(version, source_text=source_text, user_note=user_note)
            raw_output = await executor.execute(messages=messages, version=version)
        else:
            raise RuntimeError("Pipeline version has an unsupported execution_mode")
        return await self.materialize_item(
            session,
            storage,
            release_id=release_id,
            canonical_paper_id=canonical_paper_id,
            raw_output=raw_output,
            actor_principal_id=actor_principal_id,
        )

    async def execute_release_inline(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        executor: PipelineExecutor,
        *,
        release_id: uuid.UUID,
        actor_principal_id: uuid.UUID | None = None,
    ) -> int:
        """Development entry point. Production workers call execute_item per Paper."""
        paper_ids = list(
            await session.scalars(
                select(DocumentReleaseEntry.canonical_paper_id).where(
                    DocumentReleaseEntry.release_id == release_id,
                    DocumentReleaseEntry.status == "PENDING",
                )
            )
        )
        for paper_id in paper_ids:
            await self.execute_item(
                session,
                storage,
                executor,
                release_id=release_id,
                canonical_paper_id=paper_id,
                actor_principal_id=actor_principal_id,
            )
        return len(paper_ids)

    async def publish_release(
        self,
        session: AsyncSession,
        release_id: uuid.UUID,
        *,
        actor_principal_id: uuid.UUID | None = None,
    ) -> DocumentDatabaseRelease:
        release = await session.get(DocumentDatabaseRelease, release_id, with_for_update=True)
        if release is None:
            raise LookupError("Release not found")
        database = await session.get(DocumentDatabase, release.database_id, with_for_update=True)
        if database is None or database.building_release_id != release_id:
            raise RuntimeError("Release is not the Database's active BUILDING release")
        statuses = list(
            await session.scalars(
                select(DocumentReleaseEntry.status).where(
                    DocumentReleaseEntry.release_id == release_id
                )
            )
        )
        failures = sum(status == "FAILED" for status in statuses)
        complete = sum(status in {"REUSED", "SUCCEEDED"} for status in statuses)
        release.completed_count = complete
        release.failed_count = failures
        release.completeness_report = {
            "expected": release.expected_count,
            "complete": complete,
            "failed": failures,
        }
        if len(statuses) != release.expected_count or complete != release.expected_count:
            raise RuntimeError("Release is incomplete and cannot be published")
        # Production workers may prepare this before publication. Keeping the
        # idempotent fallback here preserves the inline development path.
        release_index = await session.get(DocumentReleaseIndex, release_id)
        if release_index is None:
            release_index = await document_index_service.build_release_index(session, release_id)
        if release_index.status != "READY" or release_index.bm25_status != "READY":
            raise RuntimeError("Release index is incomplete and cannot be published")
        if release.embedding_profile and release_index.embedding_status != "READY":
            raise RuntimeError("Release embedding index is incomplete and cannot be published")
        await self._sync_library_artifact_projection(
            session,
            database=database,
            release=release,
            actor_principal_id=actor_principal_id,
        )
        now = datetime.now(UTC)
        if database.current_release_id is not None:
            old = await session.get(
                DocumentDatabaseRelease, database.current_release_id, with_for_update=True
            )
            if old is not None:
                old.status = "ARCHIVED"
                old.archived_at = now
                await document_index_service.discard_release_index(session, old.release_id)
                # The partial unique index permits one CURRENT row. Force this
                # update before promoting the new row while retaining one DB transaction.
                await session.flush()
        release.status = "CURRENT"
        release.published_at = now
        database.current_release_id = release.release_id
        database.building_release_id = None
        database.retrieval_status = release.retrieval_status
        await session.flush()
        return release

    @staticmethod
    async def _sync_library_artifact_projection(
        session: AsyncSession,
        *,
        database: DocumentDatabase,
        release: DocumentDatabaseRelease,
        actor_principal_id: uuid.UUID | None,
    ) -> None:
        """Project one Database's current Documents into canonical Artifact metadata."""
        artifact_key = f"document-database:{database.database_id}"
        old_paper_ids: set[uuid.UUID] = set()
        if database.current_release_id is not None:
            old_paper_ids = set(
                await session.scalars(
                    select(DocumentReleaseEntry.canonical_paper_id).where(
                        DocumentReleaseEntry.release_id == database.current_release_id
                    )
                )
            )
        rows = (
            await session.execute(
                select(DocumentReleaseEntry, PipelineDocument)
                .join(
                    PipelineDocument,
                    PipelineDocument.document_id == DocumentReleaseEntry.document_id,
                )
                .where(DocumentReleaseEntry.release_id == release.release_id)
            )
        ).all()
        new_paper_ids = {entry.canonical_paper_id for entry, _ in rows}
        affected_paper_ids = old_paper_ids | new_paper_ids
        existing_by_paper: dict[uuid.UUID, Artifact] = {}
        if affected_paper_ids:
            existing_by_paper = {
                artifact.canonical_paper_id: artifact
                for artifact in await session.scalars(
                    select(Artifact).where(
                        Artifact.canonical_paper_id.in_(affected_paper_ids),
                        Artifact.artifact_key == artifact_key,
                    )
                )
            }
        for paper_id in old_paper_ids - new_paper_ids:
            artifact = existing_by_paper.get(paper_id)
            if artifact is not None:
                await session.delete(artifact)

        version = await session.get(DocumentPipelineVersion, release.pipeline_version_id)
        if version is None:
            raise RuntimeError("Pipeline version is missing during Artifact projection")
        for entry, document in rows:
            provenance = {
                "projection_kind": "DOCUMENT_DATABASE_CURRENT",
                "document_database_id": str(database.database_id),
                "pipeline_id": str(version.pipeline_id),
                "pipeline_version_id": str(document.pipeline_version_id),
                "document_id": str(document.document_id),
            }
            extension = ".json" if document.media_type == "application/json" else ".md"
            filename = (
                document.display_title
                if document.display_title.casefold().endswith(extension)
                else f"{document.display_title}{extension}"
            )
            artifact = existing_by_paper.get(entry.canonical_paper_id)
            if artifact is None:
                session.add(
                    Artifact(
                        canonical_paper_id=entry.canonical_paper_id,
                        artifact_key=artifact_key,
                        artifact_type="PIPELINE_DOCUMENT",
                        blob_id=document.content_blob_id,
                        status="ACTIVE",
                        original_filename=filename,
                        media_type=document.media_type,
                        provenance=provenance,
                        source_fingerprint=document.content_sha256,
                        revision=1,
                        updated_by=actor_principal_id,
                    )
                )
                continue
            if existing_by_paper[entry.canonical_paper_id].provenance.get(
                "projection_kind"
            ) not in {None, "DOCUMENT_DATABASE_CURRENT"}:
                raise RuntimeError("Artifact key collides with a non-Document projection")
            changed = any(
                (
                    artifact.blob_id != document.content_blob_id,
                    artifact.status != "ACTIVE",
                    artifact.original_filename != filename,
                    artifact.media_type != document.media_type,
                    artifact.provenance != provenance,
                    artifact.source_fingerprint != document.content_sha256,
                )
            )
            if changed:
                artifact.artifact_type = "PIPELINE_DOCUMENT"
                artifact.blob_id = document.content_blob_id
                artifact.status = "ACTIVE"
                artifact.original_filename = filename
                artifact.media_type = document.media_type
                artifact.provenance = provenance
                artifact.source_fingerprint = document.content_sha256
                artifact.revision += 1
                artifact.updated_by = actor_principal_id
        await session.flush()

    @staticmethod
    async def _desired_manifest(
        session: AsyncSession, paper_ids: list[uuid.UUID]
    ) -> list[ManifestEntry]:
        if not paper_ids:
            return []
        rows = (
            await session.execute(
                select(Artifact, Blob)
                .join(Blob, Blob.blob_id == Artifact.blob_id)
                .where(
                    Artifact.canonical_paper_id.in_(paper_ids),
                    Artifact.artifact_type == "EXTRACTED_TEXT",
                    Artifact.status == "ACTIVE",
                    Blob.status == "AVAILABLE",
                )
            )
        ).all()
        by_paper = {artifact.canonical_paper_id: (artifact, blob) for artifact, blob in rows}
        return [
            ManifestEntry(
                canonical_paper_id=paper_id,
                source_artifact_id=by_paper[paper_id][0].artifact_id
                if paper_id in by_paper
                else None,
                source_fingerprint=(
                    by_paper[paper_id][0].source_fingerprint or by_paper[paper_id][1].sha256
                    if paper_id in by_paper
                    else "MISSING"
                ),
            )
            for paper_id in paper_ids
        ]

    @staticmethod
    async def _current_manifest(
        session: AsyncSession,
        database: DocumentDatabase,
        *,
        pipeline_version_id: uuid.UUID,
        splitter_config_hash: str,
    ) -> tuple[dict[uuid.UUID, ManifestEntry], str | None]:
        if database.current_release_id is None:
            return {}, None
        release = await session.get(DocumentDatabaseRelease, database.current_release_id)
        rows = (
            await session.execute(
                select(DocumentReleaseEntry, PipelineDocument)
                .join(
                    PipelineDocument,
                    PipelineDocument.document_id == DocumentReleaseEntry.document_id,
                )
                .where(
                    DocumentReleaseEntry.release_id == database.current_release_id,
                    PipelineDocument.pipeline_version_id == pipeline_version_id,
                    PipelineDocument.splitter_config_hash == splitter_config_hash,
                )
            )
        ).all()
        return (
            {
                item.canonical_paper_id: ManifestEntry(
                    canonical_paper_id=item.canonical_paper_id,
                    source_artifact_id=item.source_artifact_id,
                    source_fingerprint=item.source_fingerprint,
                    reusable_document_id=document.document_id,
                )
                for item, document in rows
            },
            release.target_manifest_hash if release is not None else None,
        )


document_domain_service = DocumentDomainService()
