from __future__ import annotations

import pytest

from backend.app.documents.retrieval import document_retrieval_service


def evidence(document_id: str, score: float) -> dict[str, object]:
    return {
        "document_id": document_id,
        "paper": {},
        "document": {},
        "chunks": [],
        "chunk_scores": [],
        "document_score": {
            "value": score,
            "aggregation": "MAX",
            "matched_chunk_count": 1,
        },
    }


def test_cross_database_rrf_merges_same_document_and_sorts_globally() -> None:
    values = document_retrieval_service._cross_database_fusion(  # noqa: SLF001
        [
            {
                "database_id": "database-a",
                "weight": 1.0,
                "evidence": [evidence("document-shared", 10), evidence("document-a", 9)],
            },
            {
                "database_id": "database-b",
                "weight": 1.0,
                "evidence": [evidence("document-shared", 3), evidence("document-b", 2)],
            },
        ],
        total_top_k=10,
        rrf_k=60,
    )

    assert [value["document_id"] for value in values] == [
        "document-shared",
        "document-a",
        "document-b",
    ]
    assert values[0]["cross_database_score"] == pytest.approx(2 / 61)
    assert len(values[0]["database_matches"]) == 2

