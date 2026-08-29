from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.assets.storage import ObjectStorage
from backend.app.models import (
    CanonicalIdentifier,
    CanonicalMetadata,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabaseRelease,
    DocumentIndexManifestRow,
    DocumentReleaseIndex,
    PipelineDocument,
)

from .embeddings import (
    EmbeddingClient,
    document_embedding_service,
    resolve_embedding_profile,
)
from .indexing import _analyzer_config, _term_frequencies, document_index_service


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: uuid.UUID
    score: float
    bm25_score: float | None = None
    vector_score: float | None = None


@dataclass(frozen=True)
class EvidenceDatabaseSpec:
    database_id: uuid.UUID
    top_k: int
    weight: float = 1.0


class DocumentRetrievalService:
    async def search_evidence(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        embedding_client: EmbeddingClient,
        *,
        databases: Sequence[EvidenceDatabaseSpec],
        query: str,
        mode: str,
        aggregation: str,
        total_top_k: int,
        chunk_top_k_per_document: int,
        integrate_decay: float,
        rrf_k: int,
        facet_1: str | None = None,
        facet_2: str | None = None,
    ) -> dict[str, object]:
        clean_aggregation = aggregation.strip().upper()
        if clean_aggregation not in {"MAX", "INTEGRATE"}:
            raise ValueError("Evidence aggregation must be MAX or INTEGRATE")

        database_results: list[dict[str, object]] = []
        database_statuses: list[dict[str, object]] = []
        failed = False
        for spec in databases:
            try:
                database = await session.get(DocumentDatabase, spec.database_id)
                if database is None:
                    raise LookupError("Document Database not found")
                if database.status != "ACTIVE":
                    raise LookupError("Document Database is not active")
                if database.current_release_id is None:
                    raise LookupError("Document Database has no Current Release")
                release = await session.get(
                    DocumentDatabaseRelease, database.current_release_id
                )
                if release is None or release.status != "CURRENT":
                    raise LookupError("Document Database has no published Current Release")

                # Retrieve enough Chunk candidates to aggregate Documents while keeping
                # the existing index service's bounded query behavior.
                candidate_limit = min(
                    200,
                    max(spec.top_k * chunk_top_k_per_document * 4, spec.top_k),
                )
                chunk_result = await self.search(
                    session,
                    storage,
                    embedding_client,
                    database_id=spec.database_id,
                    query=query,
                    mode=mode,
                    limit=candidate_limit,
                    facet_1=facet_1,
                    facet_2=facet_2,
                )
                raw_hits = chunk_result["hits"]
                if not isinstance(raw_hits, list):
                    raise RuntimeError("Chunk retrieval returned an invalid result")
                evidence = await self._aggregate_documents(
                    session,
                    hits=raw_hits,
                    aggregation=clean_aggregation,
                    integrate_decay=integrate_decay,
                    chunk_top_k=chunk_top_k_per_document,
                    document_top_k=spec.top_k,
                )
                for rank, value in enumerate(evidence, start=1):
                    value["database_rank"] = rank
                database_results.append(
                    {
                        "database_id": str(spec.database_id),
                        "database_name": database.name,
                        "release_id": str(release.release_id),
                        "weight": spec.weight,
                        "top_k": spec.top_k,
                        "evidence": evidence,
                    }
                )
                database_statuses.append(
                    {
                        "database_id": str(spec.database_id),
                        "status": "SUCCEEDED",
                        "error": None,
                    }
                )
            except Exception as error:
                failed = True
                database_statuses.append(
                    {
                        "database_id": str(spec.database_id),
                        "status": "FAILED",
                        "error": str(error)[:1000] or type(error).__name__,
                    }
                )

        global_evidence = (
            None
            if failed
            else self._cross_database_fusion(
                database_results, total_top_k=total_top_k, rrf_k=rrf_k
            )
        )
        return {
            "query": query.strip(),
            "mode": mode.strip().upper(),
            "aggregation": clean_aggregation,
            "facet_filters": {"facet_1": facet_1, "facet_2": facet_2},
            "chunk_top_k_per_document": chunk_top_k_per_document,
            "total_top_k": total_top_k,
            "rrf_k": rrf_k,
            "status": "PARTIAL" if failed else "SUCCEEDED",
            "database_statuses": database_statuses,
            "database_results": database_results,
            "global_evidence": global_evidence,
        }

    async def search(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        embedding_client: EmbeddingClient,
        *,
        database_id: uuid.UUID,
        query: str,
        mode: str,
        limit: int,
        facet_1: str | None = None,
        facet_2: str | None = None,
    ) -> dict[str, object]:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Search query cannot be empty")
        clean_mode = mode.strip().upper()
        if clean_mode not in {"BM25", "VECTOR", "HYBRID"}:
            raise ValueError("Search mode must be BM25, VECTOR, or HYBRID")
        database = await session.get(DocumentDatabase, database_id)
        if database is None or database.current_release_id is None:
            raise LookupError("Document Database has no Current Release")
        release = await session.get(DocumentDatabaseRelease, database.current_release_id)
        if release is None:
            raise RuntimeError("Current Document Release is missing")
        candidate_limit = max(limit, min(200, limit * 4))
        bm25 = (
            await self._bm25(
                session,
                release,
                clean_query,
                candidate_limit,
                facet_1=facet_1,
                facet_2=facet_2,
            )
            if clean_mode in {"BM25", "HYBRID"}
            else []
        )
        vector = (
            await self._vector(
                session,
                storage,
                embedding_client,
                release,
                clean_query,
                candidate_limit,
                facet_1=facet_1,
                facet_2=facet_2,
            )
            if clean_mode in {"VECTOR", "HYBRID"}
            else []
        )
        hits = self._combine(clean_mode, bm25, vector)[:limit]
        chunks = {
            chunk.chunk_id: chunk
            for chunk in await session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.chunk_id.in_([hit.chunk_id for hit in hits])
                )
            )
        }
        return {
            "database_id": str(database_id),
            "release_id": str(release.release_id),
            "mode": clean_mode,
            "hits": [
                {
                    "chunk_id": str(hit.chunk_id),
                    "document_id": str(chunks[hit.chunk_id].document_id),
                    "canonical_paper_id": str(chunks[hit.chunk_id].canonical_paper_id),
                    "ordinal": chunks[hit.chunk_id].ordinal,
                    "content": chunks[hit.chunk_id].content,
                    "facet_1": chunks[hit.chunk_id].facet_1,
                    "facet_2": chunks[hit.chunk_id].facet_2,
                    "score": hit.score,
                    "bm25_score": hit.bm25_score,
                    "vector_score": hit.vector_score,
                }
                for hit in hits
                if hit.chunk_id in chunks
            ],
        }

    async def _aggregate_documents(
        self,
        session: AsyncSession,
        *,
        hits: list[dict[str, Any]],
        aggregation: str,
        integrate_decay: float,
        chunk_top_k: int,
        document_top_k: int,
    ) -> list[dict[str, Any]]:
        by_document: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for hit in hits:
            document_id = uuid.UUID(str(hit["document_id"]))
            by_document.setdefault(document_id, []).append(hit)
        if not by_document:
            return []

        documents = {
            value.document_id: value
            for value in await session.scalars(
                select(PipelineDocument).where(
                    PipelineDocument.document_id.in_(list(by_document))
                )
            )
        }
        paper_ids = {value.canonical_paper_id for value in documents.values()}
        metadata = {
            value.canonical_paper_id: value
            for value in await session.scalars(
                select(CanonicalMetadata).where(
                    CanonicalMetadata.canonical_paper_id.in_(paper_ids)
                )
            )
        }
        identifiers: dict[uuid.UUID, list[dict[str, str]]] = {}
        for value in await session.scalars(
            select(CanonicalIdentifier).where(
                CanonicalIdentifier.canonical_paper_id.in_(paper_ids)
            )
        ):
            identifiers.setdefault(value.canonical_paper_id, []).append(
                {"scheme": value.scheme, "value": value.normalized_value}
            )

        values: list[dict[str, Any]] = []
        for document_id, document_hits in by_document.items():
            document = documents.get(document_id)
            if document is None:
                continue
            ordered_hits = sorted(
                document_hits,
                key=lambda value: (-float(value["score"]), str(value["chunk_id"])),
            )
            scores = [float(value["score"]) for value in ordered_hits]
            aggregate = (
                scores[0]
                if aggregation == "MAX"
                else sum(
                    score * (integrate_decay**position)
                    for position, score in enumerate(scores)
                )
            )
            returned_hits = ordered_hits[:chunk_top_k]
            paper_metadata = metadata.get(document.canonical_paper_id)
            values.append(
                {
                    "document_id": str(document_id),
                    "paper": {
                        "canonical_paper_id": str(document.canonical_paper_id),
                        "title": paper_metadata.title if paper_metadata else None,
                        "authors": paper_metadata.authors if paper_metadata else [],
                        "publication_year": (
                            paper_metadata.publication_year if paper_metadata else None
                        ),
                        "venue": paper_metadata.venue if paper_metadata else None,
                        "identifiers": identifiers.get(document.canonical_paper_id, []),
                    },
                    "document": {
                        "display_title": document.display_title,
                        "pipeline_version_id": str(document.pipeline_version_id),
                        "media_type": document.media_type,
                        "word_count": document.word_count,
                    },
                    "chunks": [
                        {
                            "chunk_id": str(value["chunk_id"]),
                            "ordinal": value["ordinal"],
                            "content": value["content"],
                            "facet_1": value["facet_1"],
                            "facet_2": value["facet_2"],
                        }
                        for value in returned_hits
                    ],
                    "chunk_scores": [
                        {
                            "chunk_id": str(value["chunk_id"]),
                            "ranking_score": value["score"],
                            "bm25": value["bm25_score"],
                            "embedding": value["vector_score"],
                        }
                        for value in returned_hits
                    ],
                    "document_score": {
                        "value": aggregate,
                        "aggregation": aggregation,
                        "matched_chunk_count": len(ordered_hits),
                    },
                }
            )
        return sorted(
            values,
            key=lambda value: (
                -float(value["document_score"]["value"]),
                str(value["document_id"]),
            ),
        )[:document_top_k]

    @staticmethod
    def _cross_database_fusion(
        database_results: Sequence[dict[str, object]],
        *,
        total_top_k: int,
        rrf_k: int,
    ) -> list[dict[str, Any]]:
        fused: dict[str, dict[str, Any]] = {}
        for database_result in database_results:
            database_id = str(database_result["database_id"])
            raw_weight = database_result["weight"]
            if not isinstance(raw_weight, (int, float)):
                continue
            weight = float(raw_weight)
            evidence_values = database_result["evidence"]
            if not isinstance(evidence_values, list):
                continue
            for rank, evidence in enumerate(evidence_values, start=1):
                if not isinstance(evidence, dict):
                    continue
                document_id = str(evidence["document_id"])
                contribution = weight / (rrf_k + rank)
                value = fused.setdefault(
                    document_id,
                    {
                        **evidence,
                        "cross_database_score": 0.0,
                        "database_matches": [],
                    },
                )
                value["cross_database_score"] += contribution
                value["database_matches"].append(
                    {
                        "database_id": database_id,
                        "rank": rank,
                        "weight": weight,
                        "rrf_contribution": contribution,
                        "document_score": evidence["document_score"],
                    }
                )
        return sorted(
            fused.values(),
            key=lambda value: (
                -float(value["cross_database_score"]),
                -float(value["document_score"]["value"]),
                str(value["document_id"]),
            ),
        )[:total_top_k]

    async def _bm25(
        self,
        session: AsyncSession,
        release: DocumentDatabaseRelease,
        query: str,
        limit: int,
        *,
        facet_1: str | None,
        facet_2: str | None,
    ) -> list[RetrievalHit]:
        index = await session.get(DocumentReleaseIndex, release.release_id)
        if index is None or index.bm25_status != "READY":
            raise LookupError("Current BM25 index is not ready")
        analyzer = _analyzer_config(dict(release.bm25_profile))
        _, query_terms = _term_frequencies(query, analyzer)
        if not query_terms:
            return []
        allowed = await document_index_service.facet_filter_rows(
            session,
            release.release_id,
            facet_1=facet_1,
            facet_2=facet_2,
        )
        k1 = float(release.bm25_profile.get("k1", 1.2))
        b = float(release.bm25_profile.get("b", 0.75))
        average = index.bm25_average_document_length or 1.0
        rows = list(
            await session.scalars(
                select(DocumentIndexManifestRow).where(
                    DocumentIndexManifestRow.release_id == release.release_id,
                    DocumentIndexManifestRow.row_number.in_(allowed),
                )
            )
        )
        scored: list[RetrievalHit] = []
        for row in rows:
            score = 0.0
            for term, query_frequency in query_terms.items():
                frequency = int(row.bm25_term_frequencies.get(term, 0))
                if not frequency:
                    continue
                denominator = frequency + k1 * (1.0 - b + b * row.bm25_document_length / average)
                score += (
                    float(index.bm25_inverse_document_frequencies.get(term, 0.0))
                    * frequency
                    * (k1 + 1.0)
                    / denominator
                    * query_frequency
                )
            if score > 0 and math.isfinite(score):
                scored.append(RetrievalHit(row.chunk_id, score, bm25_score=score))
        return sorted(scored, key=lambda hit: (-hit.score, str(hit.chunk_id)))[:limit]

    async def _vector(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        client: EmbeddingClient,
        release: DocumentDatabaseRelease,
        query: str,
        limit: int,
        *,
        facet_1: str | None,
        facet_2: str | None,
    ) -> list[RetrievalHit]:
        if not release.embedding_profile:
            raise LookupError("Current Release has no embedding profile")
        profile = resolve_embedding_profile(dict(release.embedding_profile))
        query_vector = (
            await client.embed([query], model=profile.model, dimensions=profile.dimensions)
        )[0]
        hits = await document_embedding_service.search_release(
            session,
            storage,
            release_id=release.release_id,
            query_vector=query_vector,
            limit=limit,
            facet_1=facet_1,
            facet_2=facet_2,
        )
        return [RetrievalHit(hit.chunk_id, hit.score, vector_score=hit.score) for hit in hits]

    @staticmethod
    def _combine(
        mode: str,
        bm25: list[RetrievalHit],
        vector: list[RetrievalHit],
    ) -> list[RetrievalHit]:
        if mode == "BM25":
            return bm25
        if mode == "VECTOR":
            return vector
        values: dict[uuid.UUID, dict[str, float | None]] = {}
        for name, hits in (("bm25", bm25), ("vector", vector)):
            for rank, hit in enumerate(hits, start=1):
                value = values.setdefault(
                    hit.chunk_id,
                    {"rrf": 0.0, "bm25": None, "vector": None},
                )
                value["rrf"] = float(value["rrf"] or 0.0) + 1.0 / (60 + rank)
                value[name] = hit.bm25_score if name == "bm25" else hit.vector_score
        return sorted(
            (
                RetrievalHit(
                    chunk_id,
                    float(value["rrf"] or 0.0),
                    bm25_score=value["bm25"],
                    vector_score=value["vector"],
                )
                for chunk_id, value in values.items()
            ),
            key=lambda hit: (-hit.score, str(hit.chunk_id)),
        )


document_retrieval_service = DocumentRetrievalService()
