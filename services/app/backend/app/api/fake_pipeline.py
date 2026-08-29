from __future__ import annotations

import asyncio
import hashlib
import random

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.app.config import get_settings

router = APIRouter(prefix="/dev/fake-pipeline", tags=["development"])
_pdf_serial_gate = asyncio.Semaphore(1)

_VOCABULARY = (
    "analysis article assay baseline catalyst cell chemical cohort computation "
    "concentration control crystal dataset diffusion domain effect electron energy "
    "experiment feature field framework function gene geometry gradient graph interface "
    "kinetic layer material matrix measurement mechanism method model molecule network "
    "observation parameter pathway performance phase preparation probability process "
    "protein protocol reaction receptor regression result sample sequence signal simulation "
    "solution solvent spectrum structure surface synthesis system temperature theory tissue "
    "training transformation treatment validation value variable vector workflow"
).split()


class FakePdfTextResponse(BaseModel):
    pdf_sha256: str
    text: str
    word_count: int


def deterministic_pdf_text(pdf: bytes, word_count: int) -> str:
    digest = hashlib.sha256(pdf).hexdigest()
    generator = random.Random(int(digest, 16))
    return " ".join(generator.choice(_VOCABULARY) for _ in range(word_count))


@router.post("/pdf-to-text", response_model=FakePdfTextResponse)
async def fake_pdf_to_text(request: Request) -> FakePdfTextResponse:
    settings = get_settings()
    if settings.env == "production":
        raise HTTPException(status_code=404, detail="Not found")
    pdf = await request.body()
    if not pdf.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="A PDF body is required")
    async with _pdf_serial_gate:
        await asyncio.sleep(settings.fake_pdf_text_latency_seconds)
        result = deterministic_pdf_text(pdf, settings.fake_pdf_text_word_count)
    return FakePdfTextResponse(
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        text=result,
        word_count=len(result.split()),
    )
