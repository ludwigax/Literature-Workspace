from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.pdf_text_test.main import SerialPdfTextService, create_app


@pytest.mark.asyncio
async def test_pdfminer_service_job_protocol_and_persisted_result(tmp_path: Path) -> None:
    app = create_app(tmp_path, extractor=lambda path: f"Extracted text from {Path(path).name}\n")
    service: SerialPdfTextService = app.state.pdf_text_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        submitted = await client.post(
            "/extract",
            files=[
                ("files", ("paper.pdf", b"%PDF-1.4 first", "application/pdf")),
                ("files", ("paper.pdf", b"%PDF-1.4 second", "application/pdf")),
            ],
        )
        assert submitted.status_code == 200, submitted.text
        payload = submitted.json()
        assert payload["status"] == "queued"
        assert payload["files"] == ["paper.pdf", "paper (2).pdf"]
        job_id = payload["job_id"]
        assert (await client.get(f"/job/{job_id}/markdown")).status_code == 409

        assert await service.process_next() is True
        assert await service.process_next() is False
        status = await client.get(f"/job/{job_id}")
        assert status.json()["status"] == "done"
        result = (await client.get(f"/job/{job_id}/markdown")).json()
        assert result["markdown"] == {
            "paper.pdf": "Extracted text from paper.pdf",
            "paper (2).pdf": "Extracted text from paper (2).pdf",
        }
        assert "<!-- file: paper.pdf -->" in result["combined"]

    restarted = SerialPdfTextService(tmp_path)
    assert (await restarted.status(job_id))["status"] == "done"
    assert (await restarted.result(job_id))["markdown"] == result["markdown"]


@pytest.mark.asyncio
async def test_pdfminer_service_rejects_invalid_batch_and_marks_empty_text_error(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path, extractor=lambda _path: "  \n")
    service: SerialPdfTextService = app.state.pdf_text_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        too_many = await client.post(
            "/extract",
            files=[
                ("files", (f"paper-{index}.pdf", b"%PDF-1.4", "application/pdf"))
                for index in range(5)
            ],
        )
        assert too_many.status_code == 422
        invalid = await client.post(
            "/extract", files={"files": ("notes.txt", b"not a pdf", "text/plain")}
        )
        assert invalid.status_code == 422
        submitted = await client.post(
            "/extract", files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")}
        )
        job_id = submitted.json()["job_id"]
        assert await service.process_next() is True
        status = (await client.get(f"/job/{job_id}")).json()
        assert status["status"] == "error"
        assert "no extractable text layer" in status["error"]
