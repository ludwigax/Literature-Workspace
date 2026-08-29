from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.app.documents.embeddings import (
    OpenAICompatibleEmbeddingClient,
    embedding_batches,
    estimate_embedding_tokens,
)


@pytest.mark.asyncio
async def test_openai_embedding_client_restores_response_index_order() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://embedding.test/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload == {
            "model": "embedding-test",
            "input": ["first", "second"],
            "dimensions": 2,
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleEmbeddingClient(
            http,
            base_url="https://embedding.test/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=2,
        )
        assert await client.embed(["first", "second"], model="embedding-test", dimensions=2) == [
            [1.0, 0.0],
            [0.0, 1.0],
        ]


@pytest.mark.asyncio
async def test_openai_embedding_client_retries_network_failure_twice(monkeypatch) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    sleep = AsyncMock()
    monkeypatch.setattr("backend.app.documents.embeddings.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleEmbeddingClient(
            http,
            base_url="https://embedding.test/v1",
            api_key=None,
            timeout_seconds=1,
            max_retries=2,
        )
        assert await client.embed(["value"], model="embedding-test", dimensions=1) == [[1.0]]
    assert attempts == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_openai_embedding_client_bisects_overloaded_batch_in_order() -> None:
    request_sizes: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        inputs = payload["input"]
        request_sizes.append(len(inputs))
        if len(inputs) > 2:
            return httpx.Response(500, json={"detail": "batch overloaded"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(value)]}
                    for index, value in enumerate(inputs)
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleEmbeddingClient(
            http,
            base_url="https://embedding.test/v1",
            api_key=None,
            timeout_seconds=1,
            max_retries=2,
        )
        result = await client.embed(["1", "2", "3", "4"], model="test", dimensions=1)
    assert request_sizes == [4, 2, 2]
    assert result == [[1.0], [2.0], [3.0], [4.0]]


def test_embedding_batches_obey_item_and_estimated_token_limits() -> None:
    texts = ["x" * 2_000 for _ in range(32)]
    batches = embedding_batches(list(range(len(texts))), texts, max_items=32, max_tokens=10_000)
    assert [position for batch in batches for position in batch] == list(range(32))
    assert all(len(batch) <= 32 for batch in batches)
    assert all(
        sum(estimate_embedding_tokens(texts[position]) for position in batch) <= 10_000
        for batch in batches
    )


def test_embedding_batches_allow_one_oversized_input() -> None:
    texts = ["界" * 20_000, "short"]
    assert embedding_batches([0, 1], texts, max_items=32, max_tokens=10_000) == [[0], [1]]
