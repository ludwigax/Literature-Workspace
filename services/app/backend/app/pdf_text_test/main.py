from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from pdfminer.high_level import extract_text

MAX_BATCH_FILES = 4
MAX_FILE_BYTES = 100 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SerialPdfTextService:
    """One durable queue with exactly one pdfminer execution slot."""

    def __init__(
        self,
        data_dir: Path,
        *,
        extractor: Callable[[str], str] = extract_text,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.extractor = extractor
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for job_dir in sorted(self.data_dir.iterdir()):
            if not job_dir.is_dir() or not (job_dir / "job.json").is_file():
                continue
            job = await asyncio.to_thread(self._read_json, job_dir / "job.json")
            if job.get("status") in {"queued", "processing"}:
                job["status"] = "queued"
                job["updated_at"] = _now()
                await asyncio.to_thread(self._write_json, job_dir / "job.json", job)
                await self.queue.put(str(job["job_id"]))
        self.worker_task = asyncio.create_task(self._run(), name="pdfminer-serial-worker")

    async def stop(self) -> None:
        if self.worker_task is None:
            return
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        self.worker_task = None

    async def submit(self, files: list[UploadFile]) -> dict[str, Any]:
        if not 1 <= len(files) <= MAX_BATCH_FILES:
            raise HTTPException(status_code=422, detail="files must contain one to four PDFs")
        job_id = uuid.uuid4().hex
        job_dir = self.data_dir / job_id
        input_dir = job_dir / "inputs"
        input_dir.mkdir(parents=True)
        names: list[str] = []
        used: set[str] = set()
        try:
            for upload in files:
                name = self._unique_pdf_name(upload.filename, used)
                data = await upload.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail=f"{name} exceeds 100 MiB")
                if b"%PDF-" not in data[:1024]:
                    raise HTTPException(status_code=422, detail=f"{name} is not a PDF")
                await asyncio.to_thread((input_dir / name).write_bytes, data)
                names.append(name)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            raise
        now = _now()
        job = {
            "job_id": job_id,
            "status": "queued",
            "files": names,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        await asyncio.to_thread(self._write_json, job_dir / "job.json", job)
        await self.queue.put(job_id)
        return job

    async def status(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        path = self.data_dir / job_id / "job.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="job not found")
        return await asyncio.to_thread(self._read_json, path)

    async def result(self, job_id: str) -> dict[str, Any]:
        job = await self.status(job_id)
        if job["status"] != "done":
            raise HTTPException(status_code=409, detail=f"job is {job['status']}")
        path = self.data_dir / job_id / "result.json"
        if not path.is_file():
            raise HTTPException(status_code=500, detail="completed job result is missing")
        return await asyncio.to_thread(self._read_json, path)

    async def process_next(self) -> bool:
        try:
            job_id = self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        try:
            await self._process(job_id)
        finally:
            self.queue.task_done()
        return True

    async def _run(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._process(job_id)
            finally:
                self.queue.task_done()

    async def _process(self, job_id: str) -> None:
        job_dir = self.data_dir / job_id
        job_path = job_dir / "job.json"
        job = await asyncio.to_thread(self._read_json, job_path)
        job["status"] = "processing"
        job["updated_at"] = _now()
        await asyncio.to_thread(self._write_json, job_path, job)
        try:
            markdown: dict[str, str] = {}
            for filename in job["files"]:
                value = await asyncio.to_thread(self.extractor, str(job_dir / "inputs" / filename))
                text = self._normalize_text(value)
                if not text:
                    raise RuntimeError(f"{filename} has no extractable text layer")
                markdown[filename] = text
            combined = "\n\n".join(
                f"<!-- file: {filename} -->\n\n{markdown[filename]}" for filename in job["files"]
            )
            await asyncio.to_thread(
                self._write_json,
                job_dir / "result.json",
                {"job_id": job_id, "markdown": markdown, "combined": combined},
            )
            job["status"] = "done"
            job["error"] = None
        except Exception as error:
            job["status"] = "error"
            job["error"] = f"{type(error).__name__}: {error}"
        job["updated_at"] = _now()
        await asyncio.to_thread(self._write_json, job_path, job)

    @staticmethod
    def _unique_pdf_name(raw: str | None, used: set[str]) -> str:
        name = PurePosixPath(str(raw or "document.pdf").replace("\\", "/")).name
        name = re.sub(r"[\x00-\x1f]", "", name).strip() or "document.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=422, detail=f"{name} is not a .pdf file")
        stem, suffix = name[:-4], name[-4:]
        candidate = name
        number = 2
        while candidate.casefold() in used:
            candidate = f"{stem} ({number}){suffix}"
            number += 1
        used.add(candidate.casefold())
        return candidate

    @staticmethod
    def _normalize_text(value: str) -> str:
        lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
        return "\n".join(lines).strip()

    @staticmethod
    def _validate_job_id(value: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise HTTPException(status_code=404, detail="job not found")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"Invalid persisted object: {path}")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def create_app(
    data_dir: Path | None = None,
    *,
    extractor: Callable[[str], str] = extract_text,
) -> FastAPI:
    service = SerialPdfTextService(
        data_dir or Path(os.getenv("PDF_TEXT_TEST_DATA_DIR", ".pdf-text-test-data")),
        extractor=extractor,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    application = FastAPI(
        title="Local pdfminer PDF Text Test Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.pdf_text_service = service

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/extract")
    async def submit(files: Annotated[list[UploadFile], File()]) -> dict[str, Any]:
        return await service.submit(files)

    @application.get("/job/{job_id}")
    async def status(job_id: str) -> dict[str, Any]:
        return await service.status(job_id)

    @application.get("/job/{job_id}/markdown")
    async def markdown(job_id: str) -> dict[str, Any]:
        return await service.result(job_id)

    return application


app = create_app()
