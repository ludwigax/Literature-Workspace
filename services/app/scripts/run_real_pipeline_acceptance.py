"""Run the five-PDF DIRECT_TEXT Document pipeline acceptance scenario.

The script deliberately exercises the browser OIDC session and administrator
HTTP API while using the migration connection only for deterministic fixture
discovery and detailed post-build assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urljoin

import httpx
from sqlalchemy import and_, func, select

from backend.app.database import migration_engine, migration_session_factory
from backend.app.models import (
    Artifact,
    CanonicalMetadata,
    DocumentChunk,
    DocumentDatabase,
    DocumentDatabaseRelease,
    DocumentIndexManifestRow,
    DocumentPipeline,
    DocumentReleaseEntry,
    DocumentReleaseIndex,
    ExternalIdentity,
    Library,
    LibraryItem,
    PipelineDocument,
)

API_PREFIX = "/api/v2"
TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "form" or self.action is not None:
            return
        values = dict(attrs)
        if values.get("id") == "kc-form-login":
            self.action = values.get("action")


@dataclass(frozen=True)
class PaperFixture:
    canonical_paper_id: uuid.UUID
    title: str
    filename: str
    verification_status: str | None


@dataclass(frozen=True)
class ReleaseSnapshot:
    release_id: str
    release_number: int
    release_status: str
    expected_documents: int
    entry_statuses: dict[str, int]
    document_count: int
    chunk_count: int
    manifest_rows: int
    bm25_status: str
    embedding_status: str
    embedding_dimensions: int | None
    direct_text_matches: int


@dataclass(frozen=True)
class PostUpdateSnapshot:
    archived_release_status: str
    archived_index_removed: bool
    current_release_id: str
    extracted_text_artifacts: int
    pipeline_document_artifacts: int


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    start = await client.get(f"{API_PREFIX}/auth/login", follow_redirects=True)
    start.raise_for_status()
    parser = _LoginFormParser()
    parser.feed(start.text)
    if not parser.action:
        raise RuntimeError("Keycloak login form was not found")
    if start.url.host in {"127.0.0.1", "localhost", "::1"}:
        for cookie in client.cookies.jar:
            cookie.secure = False
    submit = await client.post(
        urljoin(str(start.url), parser.action),
        data={"username": username, "password": password, "credentialId": ""},
        follow_redirects=False,
    )
    if submit.status_code not in {302, 303} or not submit.headers.get("location"):
        raise RuntimeError(f"Keycloak login failed: HTTP {submit.status_code}")
    callback = await client.get(submit.headers["location"], follow_redirects=False)
    if callback.status_code not in {302, 303}:
        raise RuntimeError(f"OIDC callback failed: HTTP {callback.status_code}: {callback.text}")
    session = await client.get(f"{API_PREFIX}/auth/session")
    session.raise_for_status()
    principal = session.json()["principal"]
    if principal.get("system_role") != "ADMIN":
        raise RuntimeError(f"{username} is not ADMIN: {principal.get('system_role')}")
    csrf = client.cookies.get("litv2_csrf")
    if not csrf:
        raise RuntimeError("Application did not issue a CSRF cookie")
    return csrf


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    csrf: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"X-CSRF-Token": csrf} if csrf is not None else None
    response = await client.request(method, path, json=body, headers=headers)
    if response.is_error:
        raise RuntimeError(f"{method} {path} failed: HTTP {response.status_code}: {response.text}")
    return cast(dict[str, Any], response.json())


async def _alice_papers(expected: int) -> list[PaperFixture]:
    async with migration_session_factory() as session:
        rows = await session.execute(
            select(
                LibraryItem.canonical_paper_id,
                CanonicalMetadata.title,
                Artifact.original_filename,
                Artifact.verification_status,
            )
            .join(Library, Library.library_id == LibraryItem.library_id)
            .join(ExternalIdentity, ExternalIdentity.principal_id == Library.owner_principal_id)
            .join(
                CanonicalMetadata,
                CanonicalMetadata.canonical_paper_id == LibraryItem.canonical_paper_id,
            )
            .join(
                Artifact,
                and_(
                    Artifact.canonical_paper_id == LibraryItem.canonical_paper_id,
                    Artifact.artifact_key == "pdf",
                    Artifact.artifact_type == "SOURCE_PDF",
                    Artifact.status == "ACTIVE",
                ),
            )
            .where(
                func.lower(ExternalIdentity.email) == "alice@example.test",
                Library.library_type == "PERSONAL",
                Library.status == "ACTIVE",
                LibraryItem.status == "ACTIVE",
            )
            .order_by(LibraryItem.created_at, LibraryItem.library_item_id)
        )
        papers = [
            PaperFixture(paper_id, title, filename or "paper.pdf", verification)
            for paper_id, title, filename, verification in rows
        ]
    if len(papers) != expected:
        raise RuntimeError(
            f"Alice must have exactly {expected} active Items with canonical PDFs; "
            f"found {len(papers)}"
        )
    return papers


async def _require_clean_document_system() -> None:
    async with migration_session_factory() as session:
        pipeline_count = int(
            await session.scalar(select(func.count()).select_from(DocumentPipeline)) or 0
        )
        database_count = int(
            await session.scalar(select(func.count()).select_from(DocumentDatabase)) or 0
        )
    if pipeline_count or database_count:
        raise RuntimeError(
            "Document Pipelines or Databases already exist; run this acceptance test on a clean DB"
        )


async def _wait_for_run(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    previous: tuple[str, str] | None = None
    while time.monotonic() < deadline:
        details = await _request(client, "GET", f"{API_PREFIX}/document-build-runs/{run_id}")
        run = details["run"]
        state = (run["status"], run["phase"])
        if state != previous:
            print(f"run {run_id}: status={state[0]} phase={state[1]}", flush=True)
            previous = state
        if run["status"] in TERMINAL_RUN_STATES:
            if run["status"] != "SUCCEEDED":
                raise RuntimeError(
                    "Document build failed:\n" + json.dumps(details, ensure_ascii=False, indent=2)
                )
            return details
        await asyncio.sleep(poll_seconds)
    raise TimeoutError(f"Document build {run_id} exceeded {timeout_seconds:.0f} seconds")


async def _release_snapshot(database_id: uuid.UUID, release_id: uuid.UUID) -> ReleaseSnapshot:
    async with migration_session_factory() as session:
        database = await session.get(DocumentDatabase, database_id)
        release = await session.get(DocumentDatabaseRelease, release_id)
        index = await session.get(DocumentReleaseIndex, release_id)
        if database is None or release is None or index is None:
            raise RuntimeError("Published Database, Release, or index is missing")
        if database.current_release_id != release_id or database.building_release_id is not None:
            raise RuntimeError("Release publication did not atomically update Current/Building")
        entries = list(
            await session.scalars(
                select(DocumentReleaseEntry).where(DocumentReleaseEntry.release_id == release_id)
            )
        )
        document_ids = [entry.document_id for entry in entries if entry.document_id is not None]
        chunk_count = int(
            await session.scalar(
                select(func.count(DocumentChunk.chunk_id)).where(
                    DocumentChunk.document_id.in_(document_ids)
                )
            )
            or 0
        )
        manifest_count = int(
            await session.scalar(
                select(func.count(DocumentIndexManifestRow.row_number)).where(
                    DocumentIndexManifestRow.release_id == release_id
                )
            )
            or 0
        )
        direct_matches = int(
            await session.scalar(
                select(func.count(PipelineDocument.document_id))
                .join(Artifact, Artifact.artifact_id == PipelineDocument.source_artifact_id)
                .join(
                    DocumentReleaseEntry,
                    and_(
                        DocumentReleaseEntry.release_id == release_id,
                        DocumentReleaseEntry.document_id == PipelineDocument.document_id,
                    ),
                )
                .where(PipelineDocument.raw_output_blob_id == Artifact.blob_id)
            )
            or 0
        )
        snapshot = ReleaseSnapshot(
            release_id=str(release_id),
            release_number=release.release_number,
            release_status=release.status,
            expected_documents=release.expected_count,
            entry_statuses=dict(Counter(entry.status for entry in entries)),
            document_count=len(document_ids),
            chunk_count=chunk_count,
            manifest_rows=manifest_count,
            bm25_status=index.bm25_status,
            embedding_status=index.embedding_status,
            embedding_dimensions=index.embedding_dimensions,
            direct_text_matches=direct_matches,
        )
    if snapshot.release_status != "CURRENT":
        raise RuntimeError(f"Release is not CURRENT: {snapshot.release_status}")
    if snapshot.document_count != snapshot.expected_documents:
        raise RuntimeError("Release Document count is incomplete")
    if snapshot.chunk_count <= 0 or snapshot.manifest_rows != snapshot.chunk_count:
        raise RuntimeError("Chunk manifest is empty or inconsistent")
    if snapshot.bm25_status != "READY" or snapshot.embedding_status != "READY":
        raise RuntimeError("BM25 or embedding index is not READY")
    if snapshot.embedding_dimensions != 1024:
        raise RuntimeError(f"Unexpected embedding dimensions: {snapshot.embedding_dimensions}")
    if snapshot.direct_text_matches != snapshot.document_count:
        raise RuntimeError("DIRECT_TEXT Document content does not match extracted PDF text")
    return snapshot


async def _post_update_snapshot(
    database_id: uuid.UUID,
    archived_release_id: uuid.UUID,
    current_release_id: uuid.UUID,
    paper_ids: list[uuid.UUID],
) -> PostUpdateSnapshot:
    async with migration_session_factory() as session:
        database = await session.get(DocumentDatabase, database_id)
        archived = await session.get(DocumentDatabaseRelease, archived_release_id)
        if database is None or archived is None:
            raise RuntimeError("Document Database or archived Release is missing")
        extracted = int(
            await session.scalar(
                select(func.count(Artifact.artifact_id)).where(
                    Artifact.canonical_paper_id.in_(paper_ids),
                    Artifact.artifact_type == "EXTRACTED_TEXT",
                    Artifact.status == "ACTIVE",
                )
            )
            or 0
        )
        projected = int(
            await session.scalar(
                select(func.count(Artifact.artifact_id)).where(
                    Artifact.canonical_paper_id.in_(paper_ids),
                    Artifact.artifact_type == "PIPELINE_DOCUMENT",
                    Artifact.status == "ACTIVE",
                )
            )
            or 0
        )
        snapshot = PostUpdateSnapshot(
            archived_release_status=archived.status,
            archived_index_removed=(
                await session.get(DocumentReleaseIndex, archived_release_id) is None
            ),
            current_release_id=str(database.current_release_id),
            extracted_text_artifacts=extracted,
            pipeline_document_artifacts=projected,
        )
    if snapshot.archived_release_status != "ARCHIVED":
        raise RuntimeError("The first Release was not archived after publication")
    if not snapshot.archived_index_removed:
        raise RuntimeError("The archived Release retained a disposable index")
    if snapshot.current_release_id != str(current_release_id):
        raise RuntimeError("The updated Release is not the Database Current Release")
    if snapshot.extracted_text_artifacts != len(paper_ids):
        raise RuntimeError("Canonical extracted-text Artifact projection is incomplete")
    if snapshot.pipeline_document_artifacts != len(paper_ids):
        raise RuntimeError("Canonical Pipeline Document Artifact projection is incomplete")
    return snapshot


async def _run(args: argparse.Namespace) -> None:
    await _require_clean_document_system()
    papers = await _alice_papers(args.expected_papers)
    print("Verified input fixtures:")
    for number, paper in enumerate(papers, start=1):
        print(f"  {number}. {paper.filename} | {paper.title}")

    async with httpx.AsyncClient(base_url=args.api, timeout=60.0) as client:
        csrf = await _login(client, args.username, args.password)
        for paper in papers:
            await _request(
                client,
                "PATCH",
                f"{API_PREFIX}/admin/canonical-papers/{paper.canonical_paper_id}/pdf-verification",
                csrf=csrf,
                body={"verification_status": "VERIFIED"},
            )

        created = await _request(
            client,
            "POST",
            f"{API_PREFIX}/document-pipelines",
            csrf=csrf,
            body={
                "name": "PDF full text, 500-word chunks",
                "description": "Acceptance: canonical PDF text without LLM transformation",
                "initial_version": {
                    "system_prompt": "",
                    "user_prompt": "",
                    "model": "",
                    "model_config": {},
                    "input_config": {
                        "source": "canonical_pdf_text",
                        "execution_mode": "DIRECT_TEXT",
                    },
                    "splitter_type": "PARAGRAPH",
                    "splitter_config": {"chunk_size_words": 500},
                },
            },
        )
        pipeline = created["pipeline"]
        version = created["active_version"]
        database = await _request(
            client,
            "POST",
            f"{API_PREFIX}/document-databases",
            csrf=csrf,
            body={
                "pipeline_id": pipeline["pipeline_id"],
                "name": "Canonical PDF full-text corpus",
                "description": "Five-PDF two-stage acceptance corpus",
                "range_mode": "EXPLICIT",
                "bm25_profile": {
                    "lowercase": True,
                    "min_token_length": 1,
                    "stopwords": [],
                    "k1": 1.2,
                    "b": 0.75,
                },
            },
        )
        database_id = database["database_id"]
        first_ids = [str(paper.canonical_paper_id) for paper in papers[:4]]
        await _request(
            client,
            "PUT",
            f"{API_PREFIX}/document-databases/{database_id}/scope",
            csrf=csrf,
            body={"canonical_paper_ids": first_ids},
        )
        first_run = await _request(
            client,
            "POST",
            f"{API_PREFIX}/document-databases/{database_id}/reconcile",
            csrf=csrf,
            body={"build_mode": "FULL"},
        )
        first_details = await _wait_for_run(
            client,
            first_run["run_id"],
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
        )
        first_release_id = uuid.UUID(first_details["run"]["release_id"])
        first_snapshot = await _release_snapshot(uuid.UUID(database_id), first_release_id)
        if first_snapshot.expected_documents != 4:
            raise RuntimeError("First Release did not contain exactly four Papers")

        range_update = await _request(
            client,
            "PATCH",
            f"{API_PREFIX}/document-databases/{database_id}",
            csrf=csrf,
            body={"range_mode": "ALL_VERIFIED"},
        )
        second_run = await _request(
            client,
            "POST",
            f"{API_PREFIX}/document-databases/{database_id}/reconcile",
            csrf=csrf,
            body={"build_mode": "UPDATE"},
        )
        second_details = await _wait_for_run(
            client,
            second_run["run_id"],
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
        )
        second_release_id = uuid.UUID(second_details["run"]["release_id"])
        second_snapshot = await _release_snapshot(uuid.UUID(database_id), second_release_id)
        if second_snapshot.expected_documents != 5:
            raise RuntimeError("Updated Release did not contain exactly five Papers")
        if second_snapshot.entry_statuses.get("REUSED") != 4:
            raise RuntimeError("Updated Release did not reuse exactly four existing Documents")
        if second_snapshot.entry_statuses.get("SUCCEEDED") != 1:
            raise RuntimeError("Updated Release did not build exactly one new Document")
        post_update = await _post_update_snapshot(
            uuid.UUID(database_id),
            first_release_id,
            second_release_id,
            [paper.canonical_paper_id for paper in papers],
        )

        search = await _request(
            client,
            "POST",
            f"{API_PREFIX}/document-databases/{database_id}/search",
            body={
                "query": papers[4].title,
                "mode": "HYBRID",
                "limit": 10,
            },
        )
        if not search["hits"]:
            raise RuntimeError("Hybrid retrieval returned no results")
        if not any(
            hit["canonical_paper_id"] == str(papers[4].canonical_paper_id) for hit in search["hits"]
        ):
            raise RuntimeError("Hybrid retrieval did not find the newly added fifth Paper")

    print(
        json.dumps(
            {
                "outcome": "PASSED",
                "pipeline_id": pipeline["pipeline_id"],
                "pipeline_version_id": version["pipeline_version_id"],
                "execution_mode": version["input_config"]["execution_mode"],
                "chunk_size_words": version["splitter_config"]["chunk_size_words"],
                "database_id": database_id,
                "range_revision_after_update": range_update["range_revision"],
                "first_release": asdict(first_snapshot),
                "updated_release": asdict(second_snapshot),
                "post_update": asdict(post_update),
                "hybrid_hit_count": len(search["hits"]),
                "new_paper_found": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8020")
    parser.add_argument("--username", default="alice")
    parser.add_argument("--password", default="alice-local")
    parser.add_argument("--expected-papers", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args()

    async def execute() -> None:
        try:
            await _run(args)
        finally:
            await migration_engine.dispose()

    asyncio.run(execute())


if __name__ == "__main__":
    main()
