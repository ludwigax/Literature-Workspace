from __future__ import annotations

import uuid
from typing import Any

from backend.app.tools.base import ToolContext
from backend.app.tools.literature import (
    DocumentGetByDoiTool,
    DocumentRetrievalTool,
)


class FakeLiteratureClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(
        self,
        method: str,
        path: str,
        context: ToolContext,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if path == "/canonical-papers/by-doi":
            return {
                "status": "FOUND",
                "canonical_paper_id": "paper-exact",
                "metadata": {"title": "Exact paper"},
                "identifiers": [{"scheme": "DOI", "value": "10.1000/exact"}],
                "documents": [{"document_id": "doc-current"}],
            }
        if path == "/documents/doc-current":
            return {"document_id": "doc-current", "content": "x" * 1500}
        if path == "/retrieval/search":
            return {"status": "SUCCEEDED", "global_evidence": [{"document_id": "doc-1"}]}
        raise AssertionError(f"unexpected Literature request: {method} {path}")


async def test_doi_tool_uses_exact_identifier_and_bounds_document_content() -> None:
    client = FakeLiteratureClient()
    tool = DocumentGetByDoiTool(client)  # type: ignore[arg-type]
    context = ToolContext(
        turn_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        runtime_config={"doi_document_max_chars": 1000},
    )
    result = await tool.execute(
        {"doi": "doi:10.1000/EXACT"},
        context,
    )

    assert result.data["status"] == "FOUND"
    documents = result.data["documents"]
    assert len(documents) == 1
    assert len(documents[0]["content"]) == 1000
    assert documents[0]["content_truncated"] is True
    assert client.calls[0][1] == "/canonical-papers/by-doi"
    assert client.calls[0][2]["params"] == {"doi": "10.1000/exact"}


async def test_retrieval_tool_wraps_multi_database_evidence_api() -> None:
    client = FakeLiteratureClient()
    tool = DocumentRetrievalTool(client)  # type: ignore[arg-type]
    database_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    result = await tool.execute(
        {
            "query": "single-cell perturbation",
            "database_ids": database_ids,
        },
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
    method, path, kwargs = client.calls[0]
    assert (method, path) == ("POST", "/retrieval/search")
    assert [entry["database_id"] for entry in kwargs["json"]["databases"]] == database_ids
    assert kwargs["json"]["total_top_k"] == 4
    assert kwargs["json"]["mode"] == "BM25"
    assert kwargs["json"]["chunk_top_k_per_document"] == 2


def test_model_schemas_do_not_expose_server_controlled_parameters() -> None:
    client = FakeLiteratureClient()
    retrieval = DocumentRetrievalTool(client)  # type: ignore[arg-type]
    doi = DocumentGetByDoiTool(client)  # type: ignore[arg-type]

    assert set(retrieval.parameters["properties"]) == {"query", "database_ids"}
    assert set(doi.parameters["properties"]) == {"doi"}
