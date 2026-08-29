from __future__ import annotations

import asyncio
from time import perf_counter

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.config import get_settings
from backend.app.main import app


@pytest.mark.asyncio
async def test_fake_pdf_api_is_deterministic_500_words_and_serial(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "fake_pdf_text_latency_seconds", 0.03)
    monkeypatch.setattr(settings, "fake_pdf_text_word_count", 500)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = perf_counter()
        first, second = await asyncio.gather(
            client.post("/api/v2/dev/fake-pipeline/pdf-to-text", content=b"%PDF-1.4\nfirst"),
            client.post("/api/v2/dev/fake-pipeline/pdf-to-text", content=b"%PDF-1.4\nsecond"),
        )
        elapsed = perf_counter() - started
        repeated = await client.post(
            "/api/v2/dev/fake-pipeline/pdf-to-text", content=b"%PDF-1.4\nfirst"
        )
        invalid = await client.post("/api/v2/dev/fake-pipeline/pdf-to-text", content=b"not a pdf")

    assert first.status_code == second.status_code == repeated.status_code == 200
    assert first.json()["word_count"] == second.json()["word_count"] == 500
    assert first.json()["text"] == repeated.json()["text"]
    assert first.json()["text"] != second.json()["text"]
    assert elapsed >= 0.05
    assert invalid.status_code == 422
