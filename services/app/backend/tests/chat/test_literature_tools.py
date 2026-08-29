from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from backend.app.chat.tools.base import ToolContext
from backend.app.chat.tools.literature import DocumentGetByDoiTool, DocumentRetrievalTool
from backend.app.models import Blob, CanonicalMetadata, CanonicalPaper


class FakeSessionContext(AbstractAsyncContextManager[Any]):
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, session: Any) -> None:
        self.session = session

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self.session)


async def test_doi_tool_queries_global_canonical_data_and_bounds_content(monkeypatch: Any) -> None:
    paper_id = uuid.uuid4()
    identifier = SimpleNamespace(
        canonical_paper_id=paper_id,
        scheme="DOI",
        original_value="10.1000/EXACT",
    )
    document = SimpleNamespace(
        document_id=uuid.uuid4(),
        pipeline_version_id=uuid.uuid4(),
        content_blob_id=uuid.uuid4(),
        display_title="Exact paper",
        media_type="text/markdown",
        content_sha256="sha",
        word_count=250,
        provenance={},
    )
    session = AsyncMock()
    session.scalar.side_effect = [identifier, document, 3]
    session.get.side_effect = lambda model, _key: {
        CanonicalPaper: SimpleNamespace(canonical_paper_id=paper_id, status="ACTIVE"),
        CanonicalMetadata: SimpleNamespace(
            title="Exact paper",
            abstract=None,
            publication_year=2025,
            work_type="article",
            venue=None,
            canonical_url=None,
            authors=[],
        ),
        Blob: SimpleNamespace(status="AVAILABLE", storage_key="doc.md", byte_size=1500),
    }[model]
    scalar_rows = AsyncMock()
    scalar_rows.__iter__.side_effect = lambda: iter([identifier])
    session.scalars.return_value = scalar_rows
    storage = SimpleNamespace(read_bytes=AsyncMock(return_value=b"x" * 1500))
    monkeypatch.setattr(
        "backend.app.chat.tools.literature.get_object_storage", lambda: storage
    )

    tool = DocumentGetByDoiTool(FakeSessionFactory(session))  # type: ignore[arg-type]
    result = await tool.execute(
        {"doi": "doi:10.1000/EXACT"},
        ToolContext(
            turn_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            runtime_config={"doi_document_max_chars": 1000},
        ),
    )

    assert result.data["status"] == "FOUND"
    assert result.data["canonical_paper_id"] == str(paper_id)
    documents = result.data["documents"]
    assert isinstance(documents, list)
    assert len(documents[0]["content"]) == 1000
    assert documents[0]["content_truncated"] is True


async def test_retrieval_tool_uses_server_controlled_parameters(monkeypatch: Any) -> None:
    search = AsyncMock(return_value={"status": "SUCCEEDED", "global_evidence": []})
    monkeypatch.setattr(
        "backend.app.chat.tools.literature.document_retrieval_service.search_evidence",
        search,
    )
    monkeypatch.setattr(
        "backend.app.chat.tools.literature.get_object_storage", lambda: object()
    )
    monkeypatch.setattr(
        "backend.app.chat.tools.literature.openai_embedding_client", lambda _http: object()
    )
    database_ids = [uuid.uuid4(), uuid.uuid4()]
    tool = DocumentRetrievalTool(FakeSessionFactory(object()))  # type: ignore[arg-type]
    result = await tool.execute(
        {"query": "single-cell perturbation", "database_ids": list(map(str, database_ids))},
        ToolContext(
            turn_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            runtime_config={
                "retrieval_mode": "BM25",
                "retrieval_top_k": 4,
                "chunk_top_k_per_document": 2,
            },
        ),
    )

    assert result.data["status"] == "SUCCEEDED"
    assert search.await_args is not None
    kwargs = search.await_args.kwargs
    assert [spec.database_id for spec in kwargs["databases"]] == database_ids
    assert kwargs["mode"] == "BM25"
    assert kwargs["total_top_k"] == 4
    assert kwargs["chunk_top_k_per_document"] == 2


def test_model_schemas_do_not_expose_server_controlled_parameters() -> None:
    session_factory = FakeSessionFactory(object())
    retrieval = DocumentRetrievalTool(session_factory)  # type: ignore[arg-type]
    doi = DocumentGetByDoiTool(session_factory)  # type: ignore[arg-type]

    assert set(retrieval.parameters["properties"]) == {"query", "database_ids"}
    assert set(doi.parameters["properties"]) == {"doi"}
