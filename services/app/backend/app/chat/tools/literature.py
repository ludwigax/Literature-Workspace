from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...assets.storage import get_object_storage
from ...documents.embeddings import openai_embedding_client
from ...documents.retrieval import EvidenceDatabaseSpec, document_retrieval_service
from ...ingestion.providers import normalize_doi
from ...models import (
    Blob,
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    DocumentChunk,
    PipelineDocument,
)
from .base import ToolContext, ToolResult, ToolSource


class DocumentRetrievalTool:
    name = "document_retrieval"
    description = (
        "Search one or more global Document Databases and return ranked evidence "
        "chunks, document IDs, paper metadata, and identifiers such as DOI."
    )
    source_type: ToolSource = "FUNCTION"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 100000},
            "database_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "description": "Document Database UUID"},
            },
        },
        "required": ["query", "database_ids"],
        "additionalProperties": False,
    }

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        raw_database_ids = arguments.get("database_ids")
        if not query:
            raise ValueError("query must not be empty")
        if not isinstance(raw_database_ids, list) or not raw_database_ids:
            raise ValueError("database_ids must not be empty")
        try:
            database_ids = [uuid.UUID(str(value)) for value in raw_database_ids]
        except ValueError as error:
            raise ValueError("database_ids must contain UUID values") from error

        mode = str(context.runtime_config["retrieval_mode"])
        top_k = int(context.runtime_config["retrieval_top_k"])
        chunk_top_k = int(context.runtime_config["chunk_top_k_per_document"])
        specs = [EvidenceDatabaseSpec(database_id=value, top_k=top_k) for value in database_ids]
        async with self.session_factory() as session, httpx.AsyncClient() as http:
            payload = await document_retrieval_service.search_evidence(
                session,
                get_object_storage(),
                openai_embedding_client(http),
                databases=specs,
                query=query,
                mode=mode,
                aggregation="MAX",
                total_top_k=top_k,
                chunk_top_k_per_document=chunk_top_k,
                integrate_decay=0.5,
                rrf_k=60,
            )
        return ToolResult(payload)


class DocumentGetByDoiTool:
    name = "document_get_by_doi"
    description = (
        "Resolve an exact DOI in the global canonical-paper catalogue and return its "
        "latest available pipeline document. Never search through a user Library."
    )
    source_type: ToolSource = "FUNCTION"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "minLength": 4, "maxLength": 500},
        },
        "required": ["doi"],
        "additionalProperties": False,
    }

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        try:
            doi = normalize_doi(str(arguments.get("doi") or ""))
        except ValueError as error:
            raise ValueError("doi is invalid") from error
        max_chars = int(context.runtime_config["doi_document_max_chars"])

        async with self.session_factory() as session:
            identifier = await session.scalar(
                select(CanonicalIdentifier).where(
                    CanonicalIdentifier.scheme == "DOI",
                    CanonicalIdentifier.normalized_value == doi,
                )
            )
            if identifier is None:
                return ToolResult({"doi": doi, "status": "NOT_FOUND", "documents": []})
            paper = await session.get(CanonicalPaper, identifier.canonical_paper_id)
            if paper is None or paper.status != "ACTIVE":
                return ToolResult({"doi": doi, "status": "NOT_FOUND", "documents": []})
            metadata = await session.get(CanonicalMetadata, paper.canonical_paper_id)
            identifiers = list(
                await session.scalars(
                    select(CanonicalIdentifier)
                    .where(CanonicalIdentifier.canonical_paper_id == paper.canonical_paper_id)
                    .order_by(
                        CanonicalIdentifier.scheme,
                        CanonicalIdentifier.normalized_value,
                    )
                )
            )
            document = await session.scalar(
                select(PipelineDocument)
                .where(PipelineDocument.canonical_paper_id == paper.canonical_paper_id)
                .order_by(PipelineDocument.created_at.desc(), PipelineDocument.document_id)
                .limit(1)
            )
            documents: list[dict[str, object]] = []
            if document is not None:
                blob = await session.get(Blob, document.content_blob_id)
                if blob is not None and blob.status == "AVAILABLE":
                    data = await get_object_storage().read_bytes(
                        blob.storage_key, blob.byte_size + 1
                    )
                    content = data.decode("utf-8")
                    chunk_count = int(
                        await session.scalar(
                            select(func.count(DocumentChunk.chunk_id)).where(
                                DocumentChunk.document_id == document.document_id
                            )
                        )
                        or 0
                    )
                    documents.append(
                        {
                            "document_id": str(document.document_id),
                            "pipeline_version_id": str(document.pipeline_version_id),
                            "display_title": document.display_title,
                            "media_type": document.media_type,
                            "content": content[:max_chars],
                            "content_truncated": len(content) > max_chars,
                            "content_sha256": document.content_sha256,
                            "word_count": document.word_count,
                            "chunk_count": chunk_count,
                            "provenance": document.provenance,
                        }
                    )

        metadata_view: dict[str, object] | None = None
        if metadata is not None:
            metadata_view = {
                "title": metadata.title,
                "abstract": metadata.abstract,
                "publication_year": metadata.publication_year,
                "work_type": metadata.work_type,
                "venue": metadata.venue,
                "canonical_url": metadata.canonical_url,
                "authors": metadata.authors,
            }
        return ToolResult(
            {
                "doi": doi,
                "canonical_paper_id": str(paper.canonical_paper_id),
                "metadata": metadata_view,
                "identifiers": [
                    {"scheme": value.scheme, "value": value.original_value}
                    for value in identifiers
                ],
                "status": "FOUND" if documents else "DOCUMENT_UNAVAILABLE",
                "documents": documents,
            }
        )
