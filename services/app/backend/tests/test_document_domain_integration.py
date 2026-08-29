from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response
from sqlalchemy import delete, func, select

from backend.app.assets.storage import StoredObject
from backend.app.config import get_settings
from backend.app.database import migration_session_factory, worker_session_factory
from backend.app.documents.embeddings import document_embedding_service
from backend.app.documents.fake_acceptance import (
    DeterministicEmbeddingClient,
    DoublingPipelineExecutor,
    FakeIndexBuilder,
    HttpPdfTextConverter,
    fake_pipeline_acceptance_coordinator,
)
from backend.app.documents.indexing import document_index_service
from backend.app.documents.job_handlers import (
    BuildDocumentTaskHandler,
    BuildEmbeddingsTaskHandler,
    BuildManifestBm25TaskHandler,
    DocumentHandlerRegistry,
    PdfToTextTaskHandler,
    PublishReleaseTaskHandler,
    ValidateReleaseTaskHandler,
)
from backend.app.documents.orchestration import (
    INDEX_QUEUE,
    PIPELINE_QUEUE,
    SOURCE_QUEUE,
    document_build_orchestrator,
)
from backend.app.documents.retrieval import EvidenceDatabaseSpec, document_retrieval_service
from backend.app.documents.service import document_domain_service
from backend.app.documents.sources import (
    HttpPdfTextConverter as AsyncBatchPdfTextConverter,
)
from backend.app.documents.sources import PdfTextResult, PdfTextSource
from backend.app.documents.worker import DocumentWorkerBackend
from backend.app.jobs.kernel import LeaseWorker
from backend.app.library_items.service import library_item_service
from backend.app.main import app
from backend.app.models import (
    Artifact,
    Blob,
    CanonicalPaper,
    DocumentBuildRun,
    DocumentBuildTask,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabaseRelease,
    DocumentIndexManifestRow,
    DocumentPipeline,
    DocumentReleaseEntry,
    DocumentReleaseIndex,
    Library,
    LibraryItem,
    PipelineDocument,
)
from backend.app.resources.service import resource_service


class MemoryStorage:
    bucket = "test"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def ensure_bucket(self) -> None:
        return None

    async def put(
        self, key: str, stream: io.BytesIO, byte_size: int, media_type: str
    ) -> StoredObject:
        data = stream.read()
        self.objects[key] = data
        return StoredObject(self.bucket, key, len(data), None, media_type)

    async def promote(self, staging_key: str, content_key: str) -> StoredObject:
        self.objects[content_key] = self.objects.pop(staging_key)
        return StoredObject(self.bucket, content_key, len(self.objects[content_key]), None, None)

    async def stat(self, key: str) -> StoredObject | None:
        data = self.objects.get(key)
        return StoredObject(self.bucket, key, len(data), None, None) if data is not None else None

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def read_bytes(self, key: str, max_bytes: int) -> bytes:
        return self.objects[key][:max_bytes]

    async def presigned_get(self, key: str, expires: timedelta) -> str:
        return f"memory://{key}"


class ImmediatePdfTextConverter:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def submit(self, sources: list[PdfTextSource]) -> str:
        self.batches.append([source.filename for source in sources])
        return f"immediate-{len(self.batches)}"

    async def wait_and_fetch(
        self, job_id: str, sources: list[PdfTextSource]
    ) -> list[PdfTextResult]:
        del job_id
        return [
            PdfTextResult(
                filename=source.filename,
                pdf_sha256=source.pdf_sha256,
                text="alpha beta gamma delta epsilon zeta eta theta",
            )
            for source in sources
        ]


class RejectingPipelineExecutor:
    async def execute(self, **_kwargs: object) -> str:
        raise AssertionError("DIRECT_TEXT must not call the Pipeline model executor")


@pytest_asyncio.fixture
async def document_fixture() -> AsyncIterator[
    tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]]
]:
    paper_ids: list[uuid.UUID] = []
    blob_ids: list[uuid.UUID] = []
    library_ids: list[uuid.UUID] = []
    yield paper_ids, blob_ids, library_ids
    async with migration_session_factory() as session:
        pipeline_ids = list(
            await session.scalars(
                select(DocumentPipeline.pipeline_id).where(
                    DocumentPipeline.name.like("integration-document-%")
                )
            )
        )
        document_ids = list(
            await session.scalars(
                select(PipelineDocument.document_id).where(
                    PipelineDocument.canonical_paper_id.in_(paper_ids)
                )
            )
        )
        artifact_blob_ids = list(
            await session.scalars(
                select(Artifact.blob_id).where(Artifact.canonical_paper_id.in_(paper_ids))
            )
        )
        document_blob_rows = (
            await session.execute(
                select(PipelineDocument.content_blob_id, PipelineDocument.raw_output_blob_id).where(
                    PipelineDocument.canonical_paper_id.in_(paper_ids)
                )
            )
        ).all()
        embedding_blob_ids = list(
            await session.scalars(
                select(DocumentReleaseIndex.embedding_index_blob_id)
                .join(
                    DocumentDatabaseRelease,
                    DocumentDatabaseRelease.release_id == DocumentReleaseIndex.release_id,
                )
                .join(
                    DocumentDatabase,
                    DocumentDatabase.database_id == DocumentDatabaseRelease.database_id,
                )
                .where(
                    DocumentDatabase.pipeline_id.in_(pipeline_ids),
                    DocumentReleaseIndex.embedding_index_blob_id.is_not(None),
                )
            )
        )
        blob_ids.extend(artifact_blob_ids)
        blob_ids.extend(value for row in document_blob_rows for value in row)
        blob_ids.extend(value for value in embedding_blob_ids if value is not None)
        if pipeline_ids:
            await session.execute(
                delete(DocumentDatabase).where(DocumentDatabase.pipeline_id.in_(pipeline_ids))
            )
        if document_ids:
            await session.execute(
                delete(PipelineDocument).where(PipelineDocument.document_id.in_(document_ids))
            )
        if pipeline_ids:
            await session.execute(
                delete(DocumentPipeline).where(DocumentPipeline.pipeline_id.in_(pipeline_ids))
            )
        if library_ids:
            await session.execute(delete(Library).where(Library.library_id.in_(library_ids)))
        if paper_ids:
            await session.execute(
                delete(Artifact).where(Artifact.canonical_paper_id.in_(paper_ids))
            )
            await session.execute(
                delete(CanonicalPaper).where(CanonicalPaper.canonical_paper_id.in_(paper_ids))
            )
        if blob_ids:
            await session.execute(delete(Blob).where(Blob.blob_id.in_(blob_ids)))
        await session.commit()


async def add_source(
    session, paper_ids: list[uuid.UUID], blob_ids: list[uuid.UUID], marker: str
) -> CanonicalPaper:
    paper = CanonicalPaper(status="ACTIVE")
    session.add(paper)
    await session.flush()
    blob = Blob(
        sha256=marker.ljust(64, "0")[:64],
        byte_size=20,
        media_type="text/plain",
        storage_bucket="test",
        storage_key=f"source/{uuid.uuid4()}",
        status="AVAILABLE",
    )
    session.add(blob)
    await session.flush()
    session.add(
        Artifact(
            canonical_paper_id=paper.canonical_paper_id,
            artifact_key="pdf-text",
            artifact_type="EXTRACTED_TEXT",
            blob_id=blob.blob_id,
            status="ACTIVE",
            media_type="text/plain",
            source_fingerprint=marker,
        )
    )
    paper_ids.append(paper.canonical_paper_id)
    blob_ids.append(blob.blob_id)
    await session.flush()
    return paper


async def add_pdf_source(
    session,
    storage: MemoryStorage,
    paper_ids: list[uuid.UUID],
    blob_ids: list[uuid.UUID],
    marker: str,
) -> CanonicalPaper:
    paper = CanonicalPaper(status="ACTIVE")
    session.add(paper)
    await session.flush()
    pdf = f"%PDF-1.4\n{marker}\n%%EOF".encode()
    sha256 = hashlib.sha256(pdf).hexdigest()
    key = f"source/{uuid.uuid4()}"
    storage.objects[key] = pdf
    blob = Blob(
        sha256=sha256,
        byte_size=len(pdf),
        media_type="application/pdf",
        storage_bucket=storage.bucket,
        storage_key=key,
        status="AVAILABLE",
    )
    session.add(blob)
    await session.flush()
    session.add(
        Artifact(
            canonical_paper_id=paper.canonical_paper_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=blob.blob_id,
            status="ACTIVE",
            original_filename=f"{marker}.pdf",
            media_type="application/pdf",
            source_fingerprint=sha256,
        )
    )
    paper_ids.append(paper.canonical_paper_id)
    blob_ids.append(blob.blob_id)
    await session.flush()
    return paper


async def add_pdf_and_text_source(
    session,
    storage: MemoryStorage,
    paper_ids: list[uuid.UUID],
    blob_ids: list[uuid.UUID],
    marker: str,
) -> tuple[CanonicalPaper, str]:
    paper = await add_pdf_source(session, storage, paper_ids, blob_ids, marker)
    source_text = f"{marker} alpha beta gamma\n\ndelta epsilon zeta eta"
    text_bytes = source_text.encode()
    text_sha256 = hashlib.sha256(text_bytes).hexdigest()
    key = f"source/{uuid.uuid4()}"
    storage.objects[key] = text_bytes
    blob = Blob(
        sha256=text_sha256,
        byte_size=len(text_bytes),
        media_type="text/plain",
        storage_bucket=storage.bucket,
        storage_key=key,
        status="AVAILABLE",
    )
    session.add(blob)
    await session.flush()
    session.add(
        Artifact(
            canonical_paper_id=paper.canonical_paper_id,
            artifact_key="pdf-text",
            artifact_type="EXTRACTED_TEXT",
            blob_id=blob.blob_id,
            status="ACTIVE",
            media_type="text/plain",
            source_fingerprint=text_sha256,
        )
    )
    blob_ids.append(blob.blob_id)
    await session.flush()
    return paper, source_text


@pytest.mark.asyncio
async def test_async_pdf_client_submits_four_file_batch_and_polls_markdown() -> None:
    status_calls = 0

    def remote(request: Request) -> Response:
        nonlocal status_calls
        if request.method == "POST" and request.url.path == "/extract":
            body = request.content
            assert all(f"paper-{index}.pdf".encode() in body for index in range(4))
            return Response(200, json={"job_id": "remote-job-1"})
        if request.url.path == "/job/remote-job-1":
            status_calls += 1
            return Response(200, json={"status": "queued" if status_calls == 1 else "done"})
        if request.url.path == "/job/remote-job-1/markdown":
            return Response(
                200,
                json={
                    "markdown": {f"paper-{index}.pdf": f"markdown {index}" for index in range(4)},
                    "combined": "combined markdown",
                },
            )
        return Response(404)

    sources = [
        PdfTextSource(
            canonical_paper_id=uuid.uuid4(),
            source_artifact_id=uuid.uuid4(),
            filename=f"paper-{index}.pdf",
            pdf_sha256=hashlib.sha256(f"pdf-{index}".encode()).hexdigest(),
            pdf=f"%PDF-1.4 pdf-{index}".encode(),
        )
        for index in range(4)
    ]
    async with AsyncClient(transport=MockTransport(remote)) as client:
        converter = AsyncBatchPdfTextConverter(
            client,
            endpoint="http://pdf-service.test:6011",
            poll_interval_seconds=0.001,
        )
        job_id = await converter.submit(sources)
        results = await converter.wait_and_fetch(job_id, sources)
    assert job_id == "remote-job-1"
    assert status_calls == 2
    assert [result.text for result in results] == [f"markdown {index}" for index in range(4)]


@pytest.mark.asyncio
async def test_source_preparation_groups_five_pdfs_into_four_plus_one(
    document_fixture: tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    paper_ids, blob_ids, _ = document_fixture
    storage = MemoryStorage()
    async with migration_session_factory() as session:
        papers = [
            await add_pdf_source(session, storage, paper_ids, blob_ids, f"batch-source-{index}")
            for index in range(5)
        ]
        pipeline = await document_domain_service.create_pipeline(
            session, name=f"integration-document-{uuid.uuid4()}"
        )
        await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="",
            user_prompt="",
            model="",
            input_config={"execution_mode": "DIRECT_TEXT", "source": "canonical_pdf_text"},
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 250},
        )
        database = await document_domain_service.create_database(
            session, pipeline_id=pipeline.pipeline_id, name="source batch database"
        )
        await document_domain_service.replace_explicit_scope(
            session,
            database.database_id,
            {paper.canonical_paper_id for paper in papers},
        )
        run = await document_build_orchestrator.start_build(session, database.database_id)
        tasks = list(
            await session.scalars(
                select(DocumentBuildTask)
                .where(
                    DocumentBuildTask.run_id == run.run_id,
                    DocumentBuildTask.task_type == "PDF_TO_TEXT",
                )
                .order_by(DocumentBuildTask.subject_key)
            )
        )
        assert [len(task.payload["items"]) for task in tasks] == [4, 1]
        assert [task.progress_total for task in tasks] == [4, 1]
        await session.commit()


@pytest.mark.asyncio
async def test_api_style_submission_defers_release_creation_to_worker(
    document_fixture: tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    paper_ids, blob_ids, _ = document_fixture
    storage = MemoryStorage()
    async with migration_session_factory() as session:
        paper, _ = await add_pdf_and_text_source(
            session, storage, paper_ids, blob_ids, f"deferred-build-{uuid.uuid4()}"
        )
        pipeline = await document_domain_service.create_pipeline(
            session, name=f"integration-document-{uuid.uuid4()}"
        )
        await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="",
            user_prompt="",
            model="",
            input_config={"execution_mode": "DIRECT_TEXT", "source": "canonical_pdf_text"},
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 250},
        )
        database = await document_domain_service.create_database(
            session, pipeline_id=pipeline.pipeline_id, name="deferred build database"
        )
        await document_domain_service.replace_explicit_scope(
            session, database.database_id, {paper.canonical_paper_id}
        )

        run = await document_build_orchestrator.start_build(
            session, database.database_id, defer_advance=True
        )
        assert run.phase == "SOURCE_PREPARATION"
        assert run.release_id is None
        assert (
            await session.scalar(
                select(func.count(DocumentBuildTask.task_id)).where(
                    DocumentBuildTask.run_id == run.run_id
                )
            )
            == 0
        )

        assert await document_build_orchestrator.advance_submitted_runs(session) == 1
        await session.refresh(run)
        assert run.phase == "DOCUMENTS"
        assert run.release_id is not None
        document_tasks = list(
            await session.scalars(
                select(DocumentBuildTask).where(
                    DocumentBuildTask.run_id == run.run_id,
                    DocumentBuildTask.task_type == "BUILD_DOCUMENT",
                )
            )
        )
        assert len(document_tasks) == 1
        await session.commit()


@pytest.mark.asyncio
async def test_direct_text_and_all_verified_scope_reuses_explicit_release(
    document_fixture: tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    paper_ids, blob_ids, _ = document_fixture
    storage = MemoryStorage()
    async with migration_session_factory() as session:
        papers_and_text = [
            await add_pdf_and_text_source(
                session, storage, paper_ids, blob_ids, f"verified-scope-{index}"
            )
            for index in range(5)
        ]
        for paper, _ in papers_and_text:
            artifact, changed = await document_domain_service.set_pdf_verification(
                session,
                paper.canonical_paper_id,
                "VERIFIED",
                actor_principal_id=None,
            )
            assert changed is True
            assert artifact.verification_status == "VERIFIED"

        pipeline = await document_domain_service.create_pipeline(
            session, name=f"integration-document-{uuid.uuid4()}"
        )
        await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="",
            user_prompt="",
            model="",
            input_config={
                "source": "canonical_pdf_text",
                "execution_mode": "DIRECT_TEXT",
            },
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 4},
        )
        database = await document_domain_service.create_database(
            session,
            pipeline_id=pipeline.pipeline_id,
            name="verified scope database",
        )
        first_four = {paper.canonical_paper_id for paper, _ in papers_and_text[:4]}
        await document_domain_service.replace_explicit_scope(
            session, database.database_id, first_four
        )
        explicit_release = await document_domain_service.start_reconcile(
            session, database.database_id, build_mode="FULL"
        )
        assert explicit_release is not None and explicit_release.expected_count == 4
        for paper, source_text in papers_and_text[:4]:
            document = await document_domain_service.execute_item(
                session,
                storage,
                RejectingPipelineExecutor(),
                release_id=explicit_release.release_id,
                canonical_paper_id=paper.canonical_paper_id,
            )
            assert document.word_count == len(source_text.split())
            blob_ids.extend([document.content_blob_id, document.raw_output_blob_id])
        await document_domain_service.publish_release(session, explicit_release.release_id)

        previous_revision = database.range_revision
        database, changed = await document_domain_service.set_range_mode(
            session, database.database_id, "ALL_VERIFIED"
        )
        assert changed is True
        assert database.range_revision == previous_revision + 1
        assert set(await document_domain_service.resolve_scope(session, database)) == {
            paper.canonical_paper_id for paper, _ in papers_and_text
        }
        verified_release = await document_domain_service.start_reconcile(
            session, database.database_id, build_mode="UPDATE"
        )
        assert verified_release is not None and verified_release.expected_count == 5
        statuses = list(
            await session.scalars(
                select(DocumentReleaseEntry.status).where(
                    DocumentReleaseEntry.release_id == verified_release.release_id
                )
            )
        )
        assert sorted(statuses) == ["PENDING", "REUSED", "REUSED", "REUSED", "REUSED"]
        await session.commit()


@pytest.mark.asyncio
async def test_release_build_reuse_publish_archive_and_profiles(
    document_fixture: tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    paper_ids, blob_ids, library_ids = document_fixture
    storage = MemoryStorage()
    async with migration_session_factory() as session:
        first = await add_source(session, paper_ids, blob_ids, uuid.uuid4().hex)
        second = await add_source(session, paper_ids, blob_ids, uuid.uuid4().hex)
        library = Library(library_type="GROUP", name="Document projection test")
        session.add(library)
        await session.flush()
        library_ids.append(library.library_id)
        library_item = LibraryItem(
            library_id=library.library_id,
            canonical_paper_id=first.canonical_paper_id,
            item_type="PAPER",
            status="ACTIVE",
        )
        session.add(library_item)
        await session.flush()
        pipeline = await document_domain_service.create_pipeline(
            session, name=f"integration-document-{uuid.uuid4()}"
        )
        version = await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="Summarize faithfully.",
            user_prompt="Return a concise technical summary.",
            model="fake-pipeline-v1",
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 3},
        )
        database = await document_domain_service.create_database(
            session,
            pipeline_id=pipeline.pipeline_id,
            name="integration database",
            embedding_profile={
                "provider": "future",
                "model": "future-vector",
                "dimensions": 8,
                "batch_size": 2,
            },
            bm25_profile={"model": "future-bm25", "tokenizer": "future"},
        )
        await document_domain_service.replace_explicit_scope(
            session, database.database_id, {first.canonical_paper_id, second.canonical_paper_id}
        )
        release_one = await document_domain_service.start_reconcile(
            session, database.database_id, build_mode="FULL"
        )
        assert release_one is not None
        assert release_one.pipeline_version_id == version.pipeline_version_id
        assert release_one.retrieval_status == "PENDING"
        for paper in (first, second):
            document = await document_domain_service.materialize_item(
                session,
                storage,
                release_id=release_one.release_id,
                canonical_paper_id=paper.canonical_paper_id,
                raw_output="one two three\n\nfour five six",
            )
            blob_ids.extend([document.content_blob_id, document.raw_output_blob_id])
        source_chunks = list(
            await session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.canonical_paper_id.in_(paper_ids))
                .order_by(DocumentChunk.chunk_id)
            )
        )
        source_chunks[0].facet_1 = "method"
        source_chunks[0].facet_2 = "preparation"
        source_chunks[1].facet_1 = "method"
        await document_index_service.build_release_index(session, release_one.release_id)
        first_embedding_client = DeterministicEmbeddingClient()
        await document_embedding_service.build_release_embeddings(
            session,
            storage,
            first_embedding_client,
            release_id=release_one.release_id,
        )
        assert len(first_embedding_client.calls) == 2
        await document_domain_service.publish_release(session, release_one.release_id)
        await session.commit()

        first_index = await session.get(DocumentReleaseIndex, release_one.release_id)
        assert first_index is not None
        assert first_index.status == "READY"
        assert first_index.bm25_status == "READY"
        assert first_index.row_count == 4
        first_manifest = {
            row.chunk_id: row.row_number
            for row in await session.scalars(
                select(DocumentIndexManifestRow).where(
                    DocumentIndexManifestRow.release_id == release_one.release_id
                )
            )
        }
        assert sorted(first_manifest.values()) == [0, 1, 2, 3]
        assert await document_index_service.facet_filter_rows(
            session, release_one.release_id, facet_1="method"
        ) == {first_manifest[source_chunks[0].chunk_id], first_manifest[source_chunks[1].chunk_id]}
        assert await document_index_service.facet_filter_rows(
            session,
            release_one.release_id,
            facet_1="method",
            facet_2="preparation",
        ) == {first_manifest[source_chunks[0].chunk_id]}
        query_vector = (
            await first_embedding_client.embed(
                [source_chunks[0].content], model="future-vector", dimensions=8
            )
        )[0]
        search_hits = await document_embedding_service.search_release(
            session,
            storage,
            release_id=release_one.release_id,
            query_vector=query_vector,
            facet_1="method",
            facet_2="preparation",
        )
        assert [hit.chunk_id for hit in search_hits] == [source_chunks[0].chunk_id]
        hybrid = await document_retrieval_service.search(
            session,
            storage,
            first_embedding_client,
            database_id=database.database_id,
            query=source_chunks[0].content,
            mode="HYBRID",
            limit=10,
            facet_1="method",
            facet_2="preparation",
        )
        assert hybrid["release_id"] == str(release_one.release_id)
        assert [value["chunk_id"] for value in hybrid["hits"]] == [str(source_chunks[0].chunk_id)]

        evidence_search = await document_retrieval_service.search_evidence(
            session,
            storage,
            first_embedding_client,
            databases=[EvidenceDatabaseSpec(database.database_id, top_k=10)],
            query=source_chunks[0].content,
            mode="HYBRID",
            aggregation="INTEGRATE",
            total_top_k=10,
            chunk_top_k_per_document=1,
            integrate_decay=0.5,
            rrf_k=60,
            facet_1="method",
            facet_2="preparation",
        )
        assert evidence_search["status"] == "SUCCEEDED"
        assert len(evidence_search["database_results"]) == 1
        assert len(evidence_search["global_evidence"]) == 1
        evidence = evidence_search["global_evidence"][0]
        assert evidence["document_id"] == str(source_chunks[0].document_id)
        assert len(evidence["chunks"]) == 1
        assert evidence["document_score"]["aggregation"] == "INTEGRATE"
        assert evidence["database_matches"][0]["rank"] == 1

        partial_search = await document_retrieval_service.search_evidence(
            session,
            storage,
            first_embedding_client,
            databases=[
                EvidenceDatabaseSpec(database.database_id, top_k=10),
                EvidenceDatabaseSpec(uuid.uuid4(), top_k=10),
            ],
            query=source_chunks[0].content,
            mode="BM25",
            aggregation="MAX",
            total_top_k=10,
            chunk_top_k_per_document=1,
            integrate_decay=0.5,
            rrf_k=60,
            facet_1="method",
            facet_2="preparation",
        )
        assert partial_search["status"] == "PARTIAL"
        assert len(partial_search["database_results"]) == 1
        assert partial_search["global_evidence"] is None
        assert [value["status"] for value in partial_search["database_statuses"]] == [
            "SUCCEEDED",
            "FAILED",
        ]

        projection_key = f"document-database:{database.database_id}"
        first_projection = await session.scalar(
            select(Artifact).where(
                Artifact.canonical_paper_id == first.canonical_paper_id,
                Artifact.artifact_key == projection_key,
            )
        )
        assert first_projection is not None
        assert first_projection.original_filename == "integration database.md"
        assert first_projection.provenance["projection_kind"] == ("DOCUMENT_DATABASE_CURRENT")
        first_document_id = uuid.UUID(first_projection.provenance["document_id"])
        projected_blob, _ = await resource_service.projected_document_blob(
            session,
            canonical_paper_id=first.canonical_paper_id,
            provenance=first_projection.provenance,
            filename=first_projection.original_filename,
        )
        assert projected_blob.blob_id == first_projection.blob_id
        opened_blob, opened_filename = await resource_service.artifact_blob(
            session,
            library_id=library.library_id,
            library_item_id=library_item.library_item_id,
            artifact_key=projection_key,
        )
        assert opened_blob.blob_id == first_projection.blob_id
        assert opened_filename == "integration database.md"
        resources = await resource_service.catalogue(
            session,
            library_id=library.library_id,
            library_item_id=library_item.library_item_id,
        )
        assert [value["filename"] for value in resources["documents"]] == [
            "integration database.md"
        ]
        summary = await library_item_service.artifact_summary_map(session, [library_item])
        assert summary[library_item.library_item_id]["documents"] == 1

        assert (
            await session.scalar(
                select(func.count(DocumentChunk.chunk_id)).where(
                    DocumentChunk.canonical_paper_id.in_(paper_ids)
                )
            )
            == 4
        )
        assert await document_domain_service.start_reconcile(session, database.database_id) is None

        third = await add_source(session, paper_ids, blob_ids, uuid.uuid4().hex)
        await document_domain_service.replace_explicit_scope(
            session,
            database.database_id,
            {first.canonical_paper_id, second.canonical_paper_id, third.canonical_paper_id},
        )
        release_two = await document_domain_service.start_reconcile(session, database.database_id)
        assert release_two is not None
        items = list(
            await session.scalars(
                select(DocumentReleaseEntry).where(
                    DocumentReleaseEntry.release_id == release_two.release_id
                )
            )
        )
        assert sorted(item.status for item in items) == ["PENDING", "REUSED", "REUSED"]
        third_document = await document_domain_service.materialize_item(
            session,
            storage,
            release_id=release_two.release_id,
            canonical_paper_id=third.canonical_paper_id,
            raw_output="third paper output",
        )
        blob_ids.extend([third_document.content_blob_id, third_document.raw_output_blob_id])
        second_index = await document_index_service.build_release_index(
            session, release_two.release_id
        )
        assert second_index.row_count == 5
        assert (
            await session.scalar(
                select(func.count(DocumentReleaseIndex.release_id)).where(
                    DocumentReleaseIndex.release_id.in_(
                        {release_one.release_id, release_two.release_id}
                    )
                )
            )
        ) == 2
        second_manifest = {
            row.chunk_id: row.row_number
            for row in await session.scalars(
                select(DocumentIndexManifestRow).where(
                    DocumentIndexManifestRow.release_id == release_two.release_id
                )
            )
        }
        assert all(second_manifest[chunk_id] == row for chunk_id, row in first_manifest.items())
        second_embedding_client = DeterministicEmbeddingClient()
        await document_embedding_service.build_release_embeddings(
            session,
            storage,
            second_embedding_client,
            release_id=release_two.release_id,
        )
        assert sum(len(call) for call in second_embedding_client.calls) == 1
        await document_domain_service.publish_release(session, release_two.release_id)
        await session.commit()
        assert await session.get(DocumentReleaseIndex, release_one.release_id) is None
        assert await session.get(DocumentReleaseIndex, release_two.release_id) is not None

        await session.refresh(first_projection)
        assert first_projection.revision == 1
        assert uuid.UUID(first_projection.provenance["document_id"]) == first_document_id

        await session.refresh(release_one)
        await session.refresh(release_two)
        assert release_one.status == "ARCHIVED"
        assert release_two.status == "CURRENT"
        assert release_one.embedding_profile["model"] == "future-vector"

        # Shrinking a range creates a new snapshot which references only the
        # retained Paper's existing immutable Document.
        await document_domain_service.replace_explicit_scope(
            session, database.database_id, {first.canonical_paper_id}
        )
        release_three = await document_domain_service.start_reconcile(session, database.database_id)
        assert release_three is not None
        only_item = await session.scalar(
            select(DocumentReleaseEntry).where(
                DocumentReleaseEntry.release_id == release_three.release_id
            )
        )
        assert only_item is not None and only_item.status == "REUSED"
        third_index = await document_index_service.build_release_index(
            session, release_three.release_id
        )
        assert third_index.row_count == 2
        await document_embedding_service.build_release_embeddings(
            session,
            storage,
            DeterministicEmbeddingClient(),
            release_id=release_three.release_id,
        )
        await document_domain_service.publish_release(session, release_three.release_id)
        await session.commit()
        assert await session.get(DocumentReleaseIndex, release_two.release_id) is None
        assert await session.get(DocumentReleaseIndex, release_three.release_id) is not None
        remaining_projection_papers = set(
            await session.scalars(
                select(Artifact.canonical_paper_id).where(Artifact.artifact_key == projection_key)
            )
        )
        assert remaining_projection_papers == {first.canonical_paper_id}

        # A changed source fingerprint rebuilds that Paper instead of reusing it.
        source = await session.scalar(
            select(Artifact).where(
                Artifact.canonical_paper_id == first.canonical_paper_id,
                Artifact.artifact_type == "EXTRACTED_TEXT",
            )
        )
        assert source is not None
        source.source_fingerprint = uuid.uuid4().hex
        source.revision += 1
        changed_release = await document_domain_service.start_reconcile(
            session, database.database_id
        )
        assert changed_release is not None
        changed_item = await session.scalar(
            select(DocumentReleaseEntry).where(
                DocumentReleaseEntry.release_id == changed_release.release_id
            )
        )
        assert changed_item is not None and changed_item.status == "PENDING"
        changed_document = await document_domain_service.materialize_item(
            session,
            storage,
            release_id=changed_release.release_id,
            canonical_paper_id=first.canonical_paper_id,
            raw_output="changed source output",
        )
        blob_ids.extend([changed_document.content_blob_id, changed_document.raw_output_blob_id])
        await document_index_service.build_release_index(session, changed_release.release_id)
        await document_embedding_service.build_release_embeddings(
            session,
            storage,
            DeterministicEmbeddingClient(),
            release_id=changed_release.release_id,
        )
        await document_domain_service.publish_release(session, changed_release.release_id)
        await session.commit()
        await session.refresh(first_projection)
        assert first_projection.revision == 2
        assert uuid.UUID(first_projection.provenance["document_id"]) == (
            changed_document.document_id
        )
        assert await session.get(PipelineDocument, first_document_id) is not None
        historical_blob, historical_title = await resource_service.document_blob(
            session,
            library_id=library.library_id,
            library_item_id=library_item.library_item_id,
            document_id=first_document_id,
        )
        assert historical_blob.status == "AVAILABLE"
        assert historical_title == "integration database"

        # Activating Pipeline v2 changes the manifest identity and rebuilds the range.
        version_two = await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="Summarize faithfully.",
            user_prompt="Use the revised summary recipe.",
            model="fake-pipeline-v1",
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 3},
        )
        version_release = await document_domain_service.start_reconcile(
            session, database.database_id
        )
        assert version_release is not None
        assert version_release.pipeline_version_id == version_two.pipeline_version_id
        version_item = await session.scalar(
            select(DocumentReleaseEntry).where(
                DocumentReleaseEntry.release_id == version_release.release_id
            )
        )
        assert version_item is not None and version_item.status == "PENDING"
        version_document = await document_domain_service.materialize_item(
            session,
            storage,
            release_id=version_release.release_id,
            canonical_paper_id=first.canonical_paper_id,
            raw_output="pipeline version two output",
        )
        blob_ids.extend([version_document.content_blob_id, version_document.raw_output_blob_id])
        await document_index_service.build_release_index(session, version_release.release_id)
        await document_embedding_service.build_release_embeddings(
            session,
            storage,
            DeterministicEmbeddingClient(),
            release_id=version_release.release_id,
        )
        await document_domain_service.publish_release(session, version_release.release_id)
        await session.commit()
        await session.refresh(first_projection)
        assert first_projection.revision == 3
        assert uuid.UUID(first_projection.provenance["document_id"]) == (
            version_document.document_id
        )

        # A second Pipeline/Database creates another sibling projection for the
        # same Paper instead of overwriting the first Database's Artifact.
        second_pipeline = await document_domain_service.create_pipeline(
            session, name=f"integration-document-{uuid.uuid4()}"
        )
        await document_domain_service.add_pipeline_version(
            session,
            second_pipeline.pipeline_id,
            system_prompt="",
            user_prompt="Extract methods.",
            model="fake-pipeline-v1",
            splitter_type="WHOLE",
        )
        second_database = await document_domain_service.create_database(
            session,
            pipeline_id=second_pipeline.pipeline_id,
            name="methods database",
        )
        await document_domain_service.replace_explicit_scope(
            session, second_database.database_id, {first.canonical_paper_id}
        )
        second_release = await document_domain_service.start_reconcile(
            session, second_database.database_id, build_mode="FULL"
        )
        assert second_release is not None
        second_document = await document_domain_service.materialize_item(
            session,
            storage,
            release_id=second_release.release_id,
            canonical_paper_id=first.canonical_paper_id,
            raw_output="methods document",
        )
        blob_ids.extend([second_document.content_blob_id, second_document.raw_output_blob_id])
        await document_domain_service.publish_release(session, second_release.release_id)
        await session.commit()
        projections = list(
            await session.scalars(
                select(Artifact).where(
                    Artifact.canonical_paper_id == first.canonical_paper_id,
                    Artifact.artifact_type == "PIPELINE_DOCUMENT",
                    Artifact.status == "ACTIVE",
                )
            )
        )
        assert {value.original_filename for value in projections} == {
            "integration database.md",
            "methods database.md",
        }


@pytest.mark.asyncio
async def test_fake_pipeline_end_to_end_change_detection_and_library_projection(
    document_fixture: tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
    monkeypatch,
) -> None:
    paper_ids, blob_ids, library_ids = document_fixture
    storage = MemoryStorage()
    settings = get_settings()
    monkeypatch.setattr(settings, "fake_pdf_text_latency_seconds", 0.0)
    monkeypatch.setattr(settings, "fake_pdf_text_word_count", 500)
    builders = (
        FakeIndexBuilder("BM25", latency_seconds=0.0),
        FakeIndexBuilder("VECTOR", latency_seconds=0.0),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        converter = HttpPdfTextConverter(client)
        async with migration_session_factory() as session:
            first = await add_pdf_source(
                session,
                storage,
                paper_ids,
                blob_ids,
                f"acceptance-first-{uuid.uuid4()}",
            )
            second = await add_pdf_source(
                session,
                storage,
                paper_ids,
                blob_ids,
                f"acceptance-second-{uuid.uuid4()}",
            )
            library = Library(library_type="GROUP", name="Fake pipeline acceptance")
            session.add(library)
            await session.flush()
            library_ids.append(library.library_id)
            item = LibraryItem(
                library_id=library.library_id,
                canonical_paper_id=first.canonical_paper_id,
                item_type="PAPER",
                status="ACTIVE",
            )
            session.add(item)
            pipeline = await document_domain_service.create_pipeline(
                session, name=f"integration-document-{uuid.uuid4()}"
            )
            await document_domain_service.add_pipeline_version(
                session,
                pipeline.pipeline_id,
                system_prompt="Return the supplied source twice.",
                user_prompt="Echo the source twice.",
                model="fake-doubling-llm",
                splitter_type="PARAGRAPH",
                splitter_config={"chunk_size_words": 250},
            )
            database = await document_domain_service.create_database(
                session,
                pipeline_id=pipeline.pipeline_id,
                name="fake doubled documents",
                embedding_profile={"model": "fake-vector"},
                bm25_profile={"model": "fake-bm25"},
            )
            await document_domain_service.replace_explicit_scope(
                session,
                database.database_id,
                {first.canonical_paper_id, second.canonical_paper_id},
            )

            initial = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=converter,
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=builders,
                build_mode="FULL",
            )
            await session.commit()
            assert initial.outcome == "PUBLISHED"
            assert initial.converted_papers == 2
            assert initial.generated_documents == 2
            assert initial.reused_documents == 0
            assert initial.chunk_count == 8
            assert {value.kind for value in initial.indexes} == {"BM25", "VECTOR"}
            assert all(value.chunk_count == 8 for value in initial.indexes)

            resources = await resource_service.catalogue(
                session,
                library_id=library.library_id,
                library_item_id=item.library_item_id,
            )
            document_resource = resources["documents"][0]
            assert document_resource["filename"] == "fake doubled documents.md"
            document_blob, _ = await resource_service.artifact_blob(
                session,
                library_id=library.library_id,
                library_item_id=item.library_item_id,
                artifact_key=document_resource["artifact_key"],
            )
            doubled_text = (
                await storage.read_bytes(document_blob.storage_key, document_blob.byte_size + 1)
            ).decode()
            assert len(doubled_text.split()) == 1000
            first_half, second_half = doubled_text.split("\n\n")
            assert first_half == second_half

            unchanged = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=converter,
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=builders,
            )
            assert unchanged.outcome == "NO_CHANGE"
            assert unchanged.converted_papers == 0
            assert not unchanged.indexes

            third = await add_pdf_source(
                session,
                storage,
                paper_ids,
                blob_ids,
                f"acceptance-third-{uuid.uuid4()}",
            )
            await document_domain_service.replace_explicit_scope(
                session,
                database.database_id,
                {first.canonical_paper_id, second.canonical_paper_id, third.canonical_paper_id},
            )
            expanded = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=converter,
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=builders,
            )
            await session.commit()
            assert expanded.converted_papers == 1
            assert expanded.generated_documents == 1
            assert expanded.reused_documents == 2
            assert expanded.chunk_count == 12

            await document_domain_service.replace_explicit_scope(
                session, database.database_id, {first.canonical_paper_id}
            )
            reduced = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=converter,
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=builders,
            )
            await session.commit()
            assert reduced.converted_papers == 0
            assert reduced.generated_documents == 0
            assert reduced.reused_documents == 1
            assert reduced.chunk_count == 4

            replacement_pdf = f"%PDF-1.4\nacceptance-first-replaced-{uuid.uuid4()}\n%%EOF".encode()
            replacement_sha = hashlib.sha256(replacement_pdf).hexdigest()
            replacement_key = f"source/{uuid.uuid4()}"
            storage.objects[replacement_key] = replacement_pdf
            replacement_blob = Blob(
                sha256=replacement_sha,
                byte_size=len(replacement_pdf),
                media_type="application/pdf",
                storage_bucket=storage.bucket,
                storage_key=replacement_key,
                status="AVAILABLE",
            )
            session.add(replacement_blob)
            await session.flush()
            blob_ids.append(replacement_blob.blob_id)
            pdf_artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.canonical_paper_id == first.canonical_paper_id,
                    Artifact.artifact_type == "SOURCE_PDF",
                )
            )
            assert pdf_artifact is not None
            pdf_artifact.blob_id = replacement_blob.blob_id
            pdf_artifact.source_fingerprint = replacement_sha
            pdf_artifact.revision += 1
            updated = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=converter,
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=builders,
            )
            await session.commit()
            assert updated.converted_papers == 1
            assert updated.generated_documents == 1
            assert updated.reused_documents == 0
            assert updated.chunk_count == 4

            await document_domain_service.add_pipeline_version(
                session,
                pipeline.pipeline_id,
                system_prompt="Revised fake recipe.",
                user_prompt="Echo the source twice.",
                model="fake-doubling-llm",
                splitter_type="PARAGRAPH",
                splitter_config={"chunk_size_words": 250},
            )
            version_changed = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=converter,
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=builders,
            )
            await session.commit()
            assert version_changed.converted_papers == 0
            assert version_changed.generated_documents == 1
            assert version_changed.reused_documents == 0
            assert version_changed.chunk_count == 4


@pytest.mark.asyncio
async def test_durable_document_worker_runs_fixed_stage_pipeline(
    document_fixture: tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    paper_ids, blob_ids, _ = document_fixture
    storage = MemoryStorage()
    async with migration_session_factory() as session:
        paper = await add_pdf_source(
            session,
            storage,
            paper_ids,
            blob_ids,
            f"durable-worker-{uuid.uuid4()}",
        )
        pipeline = await document_domain_service.create_pipeline(
            session, name=f"integration-document-{uuid.uuid4()}"
        )
        await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="Echo the canonical text.",
            user_prompt="Return the source twice.",
            model="fake-doubling-llm",
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 4},
        )
        database = await document_domain_service.create_database(
            session,
            pipeline_id=pipeline.pipeline_id,
            name="durable worker database",
            embedding_profile={
                "model": "fake-vector",
                "dimensions": 8,
                "batch_size": 2,
            },
            bm25_profile={"lowercase": True},
        )
        await document_domain_service.replace_explicit_scope(
            session, database.database_id, {paper.canonical_paper_id}
        )
        run = await document_build_orchestrator.start_build(
            session, database.database_id, build_mode="FULL"
        )
        run_id = run.run_id
        assert run.phase == "SOURCE_PREPARATION"
        await session.commit()

    registry = DocumentHandlerRegistry()
    for handler in (
        PdfToTextTaskHandler(storage, ImmediatePdfTextConverter()),
        BuildDocumentTaskHandler(storage, DoublingPipelineExecutor()),
        BuildManifestBm25TaskHandler(),
        BuildEmbeddingsTaskHandler(storage, DeterministicEmbeddingClient()),
        ValidateReleaseTaskHandler(),
        PublishReleaseTaskHandler(),
    ):
        registry.register(handler)
    worker = LeaseWorker(
        worker_session_factory,
        DocumentWorkerBackend(
            registry,
            queue_names={SOURCE_QUEUE, PIPELINE_QUEUE, INDEX_QUEUE},
            lease_seconds=120,
        ),
        idle_seconds=0,
    )
    for _ in range(12):
        if not await worker.run_once("integration-document-worker"):
            break

    async with migration_session_factory() as session:
        completed = await session.get(DocumentBuildRun, run_id)
        assert completed is not None
        assert completed.status == "SUCCEEDED"
        assert completed.phase == "COMPLETED"
        assert completed.result is not None
        assert completed.result["outcome"] == "PUBLISHED"
        assert completed.release_id is not None
        database = await session.get(DocumentDatabase, completed.database_id)
        assert database is not None
        assert database.current_release_id == completed.release_id
        assert database.building_release_id is None
        tasks = list(
            await session.scalars(
                select(DocumentBuildTask).where(DocumentBuildTask.run_id == run_id)
            )
        )
        assert {task.task_type for task in tasks} == {
            "PDF_TO_TEXT",
            "BUILD_DOCUMENT",
            "BUILD_MANIFEST_BM25",
            "BUILD_EMBEDDINGS",
            "VALIDATE_RELEASE",
            "PUBLISH_RELEASE",
        }
        assert {task.status for task in tasks} == {"SUCCEEDED"}
