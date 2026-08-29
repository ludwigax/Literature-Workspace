from __future__ import annotations

import asyncio
import json
import uuid

from httpx import AsyncClient
from sqlalchemy import select

from backend.app.assets.service import artifact_service
from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import get_object_storage
from backend.app.database import migration_engine, migration_session_factory
from backend.app.documents.fake_acceptance import (
    DoublingPipelineExecutor,
    FakeIndexBuilder,
    HttpPdfTextConverter,
    fake_pipeline_acceptance_coordinator,
)
from backend.app.documents.service import document_domain_service
from backend.app.models import (
    CanonicalMetadata,
    CanonicalPaper,
    ExternalIdentity,
    Library,
    LibraryItem,
)
from backend.app.resources.service import resource_service


async def main() -> None:
    storage = get_object_storage()
    await storage.ensure_bucket()
    async with migration_session_factory() as session:
        alice_id = await session.scalar(
            select(ExternalIdentity.principal_id).where(
                ExternalIdentity.email == "alice@example.test"
            )
        )
        if alice_id is None:
            raise RuntimeError("alice@example.test is not initialized")
        library = await session.scalar(
            select(Library).where(
                Library.owner_principal_id == alice_id,
                Library.library_type == "PERSONAL",
                Library.status == "ACTIVE",
            )
        )
        if library is None:
            raise RuntimeError("Alice personal Library is missing")

        marker = uuid.uuid4()
        paper = CanonicalPaper(status="ACTIVE")
        session.add(paper)
        await session.flush()
        session.add(
            CanonicalMetadata(
                canonical_paper_id=paper.canonical_paper_id,
                metadata_source="UNDEFINED",
                title="Fake 60-second Pipeline Acceptance",
                work_type="REPORT",
                authors=[{"name": "Pipeline Acceptance"}],
                extra={"acceptance_marker": str(marker)},
                provenance={"source": "fake_pipeline_acceptance"},
            )
        )
        item = LibraryItem(
            library_id=library.library_id,
            canonical_paper_id=paper.canonical_paper_id,
            item_type="PAPER",
            status="ACTIVE",
            saved_by=alice_id,
        )
        session.add(item)
        pdf = f"%PDF-1.4\nFake acceptance {marker}\n%%EOF".encode()
        pdf_blob = await blob_service.store_bytes(
            session,
            storage,
            data=pdf,
            media_type="application/pdf",
            actor_principal_id=alice_id,
        )
        await artifact_service.set_canonical(
            session,
            canonical_paper_id=paper.canonical_paper_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=pdf_blob.blob_id,
            media_type="application/pdf",
            actor_principal_id=alice_id,
            original_filename="fake-pipeline-acceptance.pdf",
            provenance={"verification": "FAKE_ACCEPTANCE"},
            source_fingerprint=pdf_blob.sha256,
        )
        pipeline = await document_domain_service.create_pipeline(
            session,
            name=f"Fake doubling acceptance {marker}",
            description="60-second REST PDF text and two parallel fake index stages",
            created_by=alice_id,
        )
        await document_domain_service.add_pipeline_version(
            session,
            pipeline.pipeline_id,
            system_prompt="Return the supplied source twice.",
            user_prompt="Echo the source twice.",
            model="fake-doubling-llm",
            splitter_type="PARAGRAPH",
            splitter_config={"chunk_size_words": 250},
            created_by=alice_id,
        )
        database = await document_domain_service.create_database(
            session,
            pipeline_id=pipeline.pipeline_id,
            name="Fake doubled 500-word PDF text",
            description="Visible acceptance Document Database",
            embedding_profile={"model": "fake-vector"},
            bm25_profile={"model": "fake-bm25"},
            created_by=alice_id,
        )
        await document_domain_service.replace_explicit_scope(
            session,
            database.database_id,
            {paper.canonical_paper_id},
            actor_principal_id=alice_id,
        )
        await session.commit()

        async with AsyncClient(base_url="http://127.0.0.1:8020", timeout=180.0) as client:
            result = await fake_pipeline_acceptance_coordinator.run(
                session,
                storage,
                database_id=database.database_id,
                pdf_converter=HttpPdfTextConverter(client),
                pipeline_executor=DoublingPipelineExecutor(),
                index_builders=(
                    FakeIndexBuilder("BM25", latency_seconds=60.0),
                    FakeIndexBuilder("VECTOR", latency_seconds=60.0),
                ),
                build_mode="FULL",
            )
        await session.commit()
        resources = await resource_service.catalogue(
            session,
            library_id=library.library_id,
            library_item_id=item.library_item_id,
        )
        document_resource = resources["documents"][0]
        document_blob, _ = await resource_service.artifact_blob(
            session,
            library_id=library.library_id,
            library_item_id=item.library_item_id,
            artifact_key=str(document_resource["artifact_key"]),
        )
        document_text = (
            await storage.read_bytes(document_blob.storage_key, document_blob.byte_size + 1)
        ).decode()
        print(
            json.dumps(
                {
                    "result": {
                        "outcome": result.outcome,
                        "release_id": str(result.release_id),
                        "converted_papers": result.converted_papers,
                        "generated_documents": result.generated_documents,
                        "reused_documents": result.reused_documents,
                        "chunk_count": result.chunk_count,
                        "indexes": [value.kind for value in result.indexes],
                    },
                    "library_id": str(library.library_id),
                    "library_item_id": str(item.library_item_id),
                    "canonical_paper_id": str(paper.canonical_paper_id),
                    "database_id": str(database.database_id),
                    "document_id": document_resource.get("document_id"),
                    "document_filename": document_resource["filename"],
                    "document_word_count": len(document_text.split()),
                    "document_is_exact_double": document_text.split("\n\n")[0]
                    == document_text.split("\n\n")[1],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    await migration_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
