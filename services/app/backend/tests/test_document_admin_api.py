from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from backend.app.config import get_settings
from backend.app.database import migration_session_factory, session_factory
from backend.app.documents.retrieval import document_retrieval_service
from backend.app.identity.oidc import OidcIdentity
from backend.app.identity.service import BrowserSession, IdentityService
from backend.app.main import app
from backend.app.models import (
    Artifact,
    AuditEvent,
    Blob,
    CanonicalIdentifier,
    CanonicalPaper,
    DocumentDatabase,
    DocumentPipeline,
    ExternalIdentity,
    Library,
    LibraryMembership,
    Principal,
    PrincipalSystemRole,
    WebSession,
)


@pytest_asyncio.fixture
async def api_prefix() -> AsyncIterator[str]:
    prefix = f"document-api-{uuid.uuid4()}"
    yield prefix
    async with migration_session_factory() as session:
        pipeline_ids = list(
            await session.scalars(
                select(DocumentPipeline.pipeline_id).where(DocumentPipeline.name.like(f"{prefix}%"))
            )
        )
        if pipeline_ids:
            await session.execute(
                delete(DocumentDatabase).where(DocumentDatabase.pipeline_id.in_(pipeline_ids))
            )
            await session.execute(
                delete(DocumentPipeline).where(DocumentPipeline.pipeline_id.in_(pipeline_ids))
            )
        principal_ids = list(
            await session.scalars(
                select(ExternalIdentity.principal_id).where(
                    ExternalIdentity.subject.like(f"{prefix}%")
                )
            )
        )
        if principal_ids:
            library_ids = list(
                await session.scalars(
                    select(LibraryMembership.library_id).where(
                        LibraryMembership.principal_id.in_(principal_ids)
                    )
                )
            )
            await session.execute(
                delete(AuditEvent).where(
                    (AuditEvent.actor_principal_id.in_(principal_ids))
                    | (AuditEvent.subject_principal_id.in_(principal_ids))
                )
            )
            await session.execute(
                delete(WebSession).where(WebSession.principal_id.in_(principal_ids))
            )
            await session.execute(
                delete(LibraryMembership).where(LibraryMembership.principal_id.in_(principal_ids))
            )
            await session.execute(
                delete(ExternalIdentity).where(ExternalIdentity.principal_id.in_(principal_ids))
            )
            await session.execute(delete(Library).where(Library.library_id.in_(library_ids)))
            await session.execute(
                delete(Principal).where(Principal.principal_id.in_(principal_ids))
            )
        await session.commit()


async def provision(prefix: str, role: str) -> BrowserSession:
    service = IdentityService(get_settings())
    async with session_factory() as session:
        principal = await service._provision_principal(  # noqa: SLF001
            session,
            OidcIdentity(
                issuer=get_settings().oidc_issuer,
                subject=prefix,
                display_name=prefix,
                email=f"{prefix}@example.test",
            ),
        )
        assignment = await session.get(PrincipalSystemRole, principal.principal_id)
        assert assignment is not None
        assignment.role = role
        record, browser = service._new_browser_session(principal, None)  # noqa: SLF001
        session.add(record)
        await session.commit()
        return browser


def client(browser: BrowserSession) -> AsyncClient:
    settings = get_settings()
    value = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    value.cookies.set(settings.session_cookie_name, browser.token)
    value.cookies.set(settings.csrf_cookie_name, browser.csrf_token)
    return value


def initial_pipeline(prefix: str) -> dict[str, object]:
    return {
        "name": f"{prefix}-pipeline",
        "description": "Document API integration",
        "initial_version": {
            "system_prompt": "Summarize faithfully.",
            "user_prompt": "Return a concise summary.",
            "model": "fake-model",
            "model_config": {},
            "input_config": {"source": "canonical_pdf_text"},
            "splitter_type": "PARAGRAPH",
            "splitter_config": {"chunk_size_words": 250},
        },
    }


@pytest.mark.asyncio
async def test_user_reads_document_admin_surface_but_cannot_mutate(api_prefix: str) -> None:
    browser = await provision(f"{api_prefix}-user", "USER")
    async with client(browser) as http:
        session = await http.get("/api/v2/auth/session")
        assert session.status_code == 200
        assert session.json()["principal"]["system_role"] == "USER"
        assert (await http.get("/api/v2/document-pipelines")).status_code == 200
        denied = await http.post(
            "/api/v2/document-pipelines",
            json=initial_pipeline(api_prefix),
            headers={"X-CSRF-Token": browser.csrf_token},
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_user_can_submit_multi_database_evidence_search(
    api_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await provision(f"{api_prefix}-retrieval-user", "USER")
    first_database_id = uuid.uuid4()
    second_database_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_search_evidence(*args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "status": "SUCCEEDED",
            "database_statuses": [],
            "database_results": [],
            "global_evidence": [],
        }

    monkeypatch.setattr(document_retrieval_service, "search_evidence", fake_search_evidence)
    async with client(browser) as http:
        response = await http.post(
            "/api/v2/retrieval/search",
            json={
                "query": "preparation method",
                "databases": [
                    {"database_id": str(first_database_id), "top_k": 12, "weight": 2},
                    {"database_id": str(second_database_id)},
                ],
                "database_top_k": 7,
                "total_top_k": 15,
                "chunk_top_k_per_document": 3,
                "aggregation": "INTEGRATE",
                "integrate_decay": 0.4,
                "facet_1": "method",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "SUCCEEDED"
        specs = captured["databases"]
        assert isinstance(specs, list)
        assert [(value.database_id, value.top_k, value.weight) for value in specs] == [
            (first_database_id, 12, 2.0),
            (second_database_id, 7, 1.0),
        ]
        assert captured["total_top_k"] == 15
        assert captured["chunk_top_k_per_document"] == 3
        assert captured["aggregation"] == "INTEGRATE"

        duplicate = await http.post(
            "/api/v2/retrieval/search",
            json={
                "query": "duplicate",
                "databases": [
                    {"database_id": str(first_database_id)},
                    {"database_id": str(first_database_id)},
                ],
            },
        )
        assert duplicate.status_code == 422


@pytest.mark.asyncio
async def test_user_resolves_global_canonical_paper_by_exact_doi_without_library(
    api_prefix: str,
) -> None:
    browser = await provision(f"{api_prefix}-canonical-user", "USER")
    doi = f"10.9999/{uuid.uuid4().hex}"
    async with migration_session_factory() as session:
        paper = CanonicalPaper(status="ACTIVE")
        session.add(paper)
        await session.flush()
        session.add(
            CanonicalIdentifier(
                canonical_paper_id=paper.canonical_paper_id,
                scheme="DOI",
                normalized_value=doi,
                original_value=f"https://doi.org/{doi}",
            )
        )
        await session.commit()
        paper_id = paper.canonical_paper_id
    try:
        async with client(browser) as http:
            response = await http.get(
                "/api/v2/canonical-papers/by-doi", params={"doi": f"DOI:{doi.upper()}"}
            )
        assert response.status_code == 200, response.text
        assert response.json()["canonical_paper_id"] == str(paper_id)
        assert response.json()["documents"] == []
    finally:
        async with migration_session_factory() as session:
            await session.execute(
                delete(CanonicalIdentifier).where(
                    CanonicalIdentifier.canonical_paper_id == paper_id
                )
            )
            await session.execute(
                delete(CanonicalPaper).where(CanonicalPaper.canonical_paper_id == paper_id)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_admin_manages_pipeline_database_policy_and_build(api_prefix: str) -> None:
    browser = await provision(f"{api_prefix}-admin", "ADMIN")
    headers = {"X-CSRF-Token": browser.csrf_token}
    async with client(browser) as http:
        created = await http.post(
            "/api/v2/document-pipelines",
            json=initial_pipeline(api_prefix),
            headers=headers,
        )
        assert created.status_code == 201, created.text
        pipeline = created.json()["pipeline"]
        first_version = created.json()["active_version"]
        same = await http.post(
            f"/api/v2/document-pipelines/{pipeline['pipeline_id']}/versions",
            json=initial_pipeline(api_prefix)["initial_version"],
            headers=headers,
        )
        assert same.status_code == 201, same.text
        assert same.json()["created"] is False
        assert same.json()["version"]["version"] == first_version["version"]
        changed_body = dict(initial_pipeline(api_prefix)["initial_version"])
        changed_body["user_prompt"] = "Return a revised concise summary."
        changed = await http.post(
            f"/api/v2/document-pipelines/{pipeline['pipeline_id']}/versions",
            json=changed_body,
            headers=headers,
        )
        assert changed.status_code == 201, changed.text
        assert changed.json()["created"] is True
        assert changed.json()["version"]["version"] == 2

        database_response = await http.post(
            "/api/v2/document-databases",
            json={
                "pipeline_id": pipeline["pipeline_id"],
                "name": f"{api_prefix}-database",
                "range_mode": "EXPLICIT",
                "bm25_profile": {},
            },
            headers=headers,
        )
        assert database_response.status_code == 201, database_response.text
        database = database_response.json()
        settings = get_settings()
        assert database["embedding_profile"] == {
            "provider": "OPENAI_COMPATIBLE",
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
            "batch_size": settings.embedding_batch_size,
            "max_batch_tokens": settings.embedding_max_batch_tokens,
        }
        range_update = await http.patch(
            f"/api/v2/document-databases/{database['database_id']}",
            json={"range_mode": "ALL_VERIFIED"},
            headers=headers,
        )
        assert range_update.status_code == 200, range_update.text
        assert range_update.json()["range_mode"] == "ALL_VERIFIED"
        assert range_update.json()["range_revision"] == database["range_revision"] + 1
        scope = await http.get(f"/api/v2/document-databases/{database['database_id']}/scope")
        assert scope.status_code == 200
        assert all(uuid.UUID(value) for value in scope.json()["canonical_paper_ids"])
        assert scope.json()["explicit_canonical_paper_ids"] == []
        policy = await http.patch(
            f"/api/v2/document-databases/{database['database_id']}/reconcile-policy",
            json={"enabled": True},
            headers=headers,
        )
        assert policy.status_code == 200, policy.text
        assert policy.json()["auto_reconcile_enabled"] is True
        assert policy.json()["next_reconcile_at"] is not None

        reconcile = await http.post(
            f"/api/v2/document-databases/{database['database_id']}/reconcile",
            json={"build_mode": "FULL"},
            headers=headers,
        )
        assert reconcile.status_code == 202, reconcile.text
        run = reconcile.json()
        assert run["status"] == "RUNNING"
        assert run["phase"] == "SOURCE_PREPARATION"
        details = await http.get(f"/api/v2/document-build-runs/{run['run_id']}")
        assert details.status_code == 200
        assert details.json()["tasks"] == []
        cancelled = await http.post(
            f"/api/v2/document-build-runs/{run['run_id']}/cancel", headers=headers
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_admin_assigns_system_role_and_promoted_user_gains_access(
    api_prefix: str,
) -> None:
    admin_browser = await provision(f"{api_prefix}-role-admin", "ADMIN")
    user_browser = await provision(f"{api_prefix}-role-user", "USER")
    async with client(user_browser) as user_http:
        user_session = await user_http.get("/api/v2/auth/session")
        principal_id = user_session.json()["principal"]["principal_id"]
        assert (await user_http.get("/api/v2/admin/principals")).status_code == 403

    async with client(admin_browser) as admin_http:
        headers = {"X-CSRF-Token": admin_browser.csrf_token}
        principals = await admin_http.get("/api/v2/admin/principals")
        assert principals.status_code == 200
        promoted = await admin_http.patch(
            f"/api/v2/admin/principals/{principal_id}/system-role",
            json={"system_role": "ADMIN"},
            headers=headers,
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["system_role"] == "ADMIN"

    async with client(user_browser) as promoted_http:
        assert (await promoted_http.get("/api/v2/admin/principals")).status_code == 200


@pytest.mark.asyncio
async def test_bootstrap_admin_email_applies_when_principal_is_created(api_prefix: str) -> None:
    email = f"{api_prefix}-bootstrap@example.test"
    settings = get_settings().model_copy(update={"bootstrap_admin_emails": email.upper()})
    service = IdentityService(settings)
    async with session_factory() as session:
        principal = await service._provision_principal(  # noqa: SLF001
            session,
            OidcIdentity(
                issuer=settings.oidc_issuer,
                subject=f"{api_prefix}-bootstrap",
                display_name="Bootstrap Admin",
                email=email,
            ),
        )
        assignment = await session.get(PrincipalSystemRole, principal.principal_id)
        assert assignment is not None and assignment.role == "ADMIN"
        await session.commit()


@pytest.mark.asyncio
async def test_admin_verifies_canonical_pdf(api_prefix: str) -> None:
    browser = await provision(f"{api_prefix}-verify-admin", "ADMIN")
    async with migration_session_factory() as session:
        paper = CanonicalPaper(status="ACTIVE")
        session.add(paper)
        await session.flush()
        blob = Blob(
            sha256=uuid.uuid4().hex.ljust(64, "0"),
            byte_size=10,
            media_type="application/pdf",
            storage_bucket="test",
            storage_key=f"verification/{uuid.uuid4()}",
            status="AVAILABLE",
        )
        session.add(blob)
        await session.flush()
        artifact = Artifact(
            canonical_paper_id=paper.canonical_paper_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=blob.blob_id,
            status="ACTIVE",
            media_type="application/pdf",
            verification_status="UNVERIFIED",
        )
        session.add(artifact)
        paper_id = paper.canonical_paper_id
        blob_id = blob.blob_id
        await session.commit()

    try:
        async with client(browser) as http:
            response = await http.patch(
                f"/api/v2/admin/canonical-papers/{paper_id}/pdf-verification",
                json={"verification_status": "VERIFIED"},
                headers={"X-CSRF-Token": browser.csrf_token},
            )
            assert response.status_code == 200, response.text
            assert response.json()["verification_status"] == "VERIFIED"
            assert response.json()["changed"] is True
    finally:
        async with migration_session_factory() as session:
            await session.execute(delete(Artifact).where(Artifact.canonical_paper_id == paper_id))
            await session.execute(
                delete(CanonicalPaper).where(CanonicalPaper.canonical_paper_id == paper_id)
            )
            await session.execute(delete(Blob).where(Blob.blob_id == blob_id))
            await session.commit()
