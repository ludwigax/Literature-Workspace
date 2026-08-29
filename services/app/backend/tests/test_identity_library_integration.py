from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Literal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, ConnectTimeout, MockTransport, Request, Response
from sqlalchemy import delete, select, text

from backend.app.assets.service import artifact_service
from backend.app.authorization.dependencies import Actor
from backend.app.config import get_settings
from backend.app.database import migration_session_factory, session_factory, worker_session_factory
from backend.app.identity.oidc import OidcClient, OidcIdentity
from backend.app.identity.service import BrowserSession, IdentityService
from backend.app.ingestion.citation_import import CITATION_IMPORT_JOB, CitationImportHandler
from backend.app.ingestion.pdf_import import PDF_IMPORT_JOB, PdfImportHandler, PdfText
from backend.app.ingestion.providers import DoiMetadataResolver, ResolvedMetadata
from backend.app.ingestion.reconcile import doi_reconciliation_service
from backend.app.ingestion.service import METADATA_REFRESH_JOB, metadata_refresh_service
from backend.app.jobs.service import job_service
from backend.app.main import app
from backend.app.models import (
    Artifact,
    Asset,
    AuditEvent,
    BackgroundJob,
    Blob,
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    Collection,
    CollectionItem,
    ExternalIdentity,
    ItemArtifactOverride,
    Library,
    LibraryInvitation,
    LibraryItem,
    LibraryMembership,
    Principal,
    WebSession,
    ZoteroImportEntry,
    ZoteroImportSource,
)


@pytest_asyncio.fixture
async def identity_prefix() -> AsyncIterator[str]:
    prefix = f"integration-{uuid.uuid4()}"
    yield prefix
    async with migration_session_factory() as session:
        principal_ids = list(
            await session.scalars(
                select(ExternalIdentity.principal_id).where(
                    ExternalIdentity.subject.like(f"{prefix}%")
                )
            )
        )
        if not principal_ids:
            return
        library_ids = list(
            await session.scalars(
                select(LibraryMembership.library_id).where(
                    LibraryMembership.principal_id.in_(principal_ids)
                )
            )
        )
        canonical_ids = list(
            await session.scalars(
                select(LibraryItem.canonical_paper_id).where(
                    LibraryItem.library_id.in_(library_ids)
                )
            )
        )
        blob_ids = list(
            await session.scalars(select(Blob.blob_id).where(Blob.created_by.in_(principal_ids)))
        )
        await session.execute(
            delete(CollectionItem).where(CollectionItem.library_id.in_(library_ids))
        )
        await session.execute(delete(Collection).where(Collection.library_id.in_(library_ids)))
        await session.execute(delete(LibraryItem).where(LibraryItem.library_id.in_(library_ids)))
        await session.execute(
            delete(LibraryInvitation).where(LibraryInvitation.library_id.in_(library_ids))
        )
        await session.execute(
            delete(AuditEvent).where(
                (AuditEvent.actor_principal_id.in_(principal_ids))
                | (AuditEvent.subject_principal_id.in_(principal_ids))
            )
        )
        await session.execute(
            delete(LibraryMembership).where(LibraryMembership.principal_id.in_(principal_ids))
        )
        await session.execute(delete(WebSession).where(WebSession.principal_id.in_(principal_ids)))
        await session.execute(
            delete(ExternalIdentity).where(ExternalIdentity.principal_id.in_(principal_ids))
        )
        await session.execute(delete(Library).where(Library.library_id.in_(library_ids)))
        if canonical_ids:
            await session.execute(
                delete(CanonicalIdentifier).where(
                    CanonicalIdentifier.canonical_paper_id.in_(canonical_ids)
                )
            )
            await session.execute(
                delete(CanonicalMetadata).where(
                    CanonicalMetadata.canonical_paper_id.in_(canonical_ids)
                )
            )
            await session.execute(
                delete(CanonicalPaper).where(CanonicalPaper.canonical_paper_id.in_(canonical_ids))
            )
        if blob_ids:
            await session.execute(delete(Blob).where(Blob.blob_id.in_(blob_ids)))
        await session.execute(delete(Principal).where(Principal.principal_id.in_(principal_ids)))
        await session.commit()


async def provision_browser_session(*, subject: str, email: str, name: str) -> BrowserSession:
    service = IdentityService(get_settings())
    async with session_factory() as session:
        principal = await service._provision_principal(  # noqa: SLF001
            session,
            OidcIdentity(
                issuer=get_settings().oidc_issuer,
                subject=subject,
                display_name=name,
                email=email,
            ),
        )
        record, browser_session = service._new_browser_session(  # noqa: SLF001
            principal, None
        )
        session.add(record)
        await session.commit()
    return browser_session


def authenticated_client(browser_session: BrowserSession) -> AsyncClient:
    settings = get_settings()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.cookies.set(settings.session_cookie_name, browser_session.token)
    client.cookies.set(settings.csrf_cookie_name, browser_session.csrf_token)
    return client


@pytest.mark.asyncio
async def test_legacy_service_identity_headers_are_rejected(identity_prefix: str) -> None:
    browser_session = await provision_browser_session(
        subject=f"{identity_prefix}-chat-service",
        email=f"{identity_prefix}-chat-service@example.test",
        name="Chat Service User",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v2/libraries",
            headers={
                "X-Literature-Service-Token": "obsolete-token",
                "X-Act-As-Principal-Id": str(browser_session.principal.principal_id),
            },
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_provisioning_is_idempotent(identity_prefix: str) -> None:
    service = IdentityService(get_settings())
    identity = OidcIdentity(
        issuer=get_settings().oidc_issuer,
        subject=f"{identity_prefix}-same",
        display_name="Same User",
        email=f"{identity_prefix}@example.test",
    )
    async with session_factory() as session:
        first = await service._provision_principal(session, identity)  # noqa: SLF001
        await session.commit()
        first_id = first.principal_id
        second = await service._provision_principal(session, identity)  # noqa: SLF001
        await session.commit()
        assert second.principal_id == first_id
        await session.execute(
            text("SELECT set_config('app.principal_id', :principal_id, true)"),
            {"principal_id": str(first_id)},
        )
        personal_count = await session.scalar(
            select(Library)
            .where(
                Library.owner_principal_id == first_id,
                Library.library_type == "PERSONAL",
            )
            .with_only_columns(Library.library_id)
        )
        assert personal_count is not None


@pytest.mark.asyncio
async def test_library_isolation_group_invitation_and_csrf(
    identity_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-alice",
        email=f"{identity_prefix}-alice@example.test",
        name="Alice Integration",
    )
    bob = await provision_browser_session(
        subject=f"{identity_prefix}-bob",
        email=f"{identity_prefix}-bob@example.test",
        name="Bob Integration",
    )
    settings = get_settings()
    async with authenticated_client(alice) as alice_client, authenticated_client(bob) as bob_client:
        alice_libraries = (await alice_client.get("/api/v2/libraries")).json()["libraries"]
        bob_libraries = (await bob_client.get("/api/v2/libraries")).json()["libraries"]
        assert len(alice_libraries) == 1
        assert len(bob_libraries) == 1
        assert alice_libraries[0]["library_id"] != bob_libraries[0]["library_id"]

        async with session_factory() as rls_session:
            assert await rls_session.scalar(text("SELECT current_user")) == "literature_app"
            assert not await rls_session.scalar(
                text("SELECT has_table_privilege(current_user, 'audit_events', 'UPDATE')")
            )
            assert not await rls_session.scalar(
                text("SELECT has_table_privilege(current_user, 'audit_events', 'DELETE')")
            )
            assert list(await rls_session.scalars(select(Library))) == []
            await rls_session.execute(
                text("SELECT set_config('app.principal_id', :principal_id, true)"),
                {"principal_id": str(alice.principal.principal_id)},
            )
            visible_ids = set(await rls_session.scalars(select(Library.library_id)))
            assert uuid.UUID(alice_libraries[0]["library_id"]) in visible_ids
            assert uuid.UUID(bob_libraries[0]["library_id"]) not in visible_ids
            blocked_update = await rls_session.execute(
                text("UPDATE libraries SET name = 'RLS breach' WHERE library_id = :library_id"),
                {"library_id": bob_libraries[0]["library_id"]},
            )
            assert blocked_update.rowcount == 0
            await rls_session.rollback()

        inaccessible = await alice_client.get(f"/api/v2/libraries/{bob_libraries[0]['library_id']}")
        assert inaccessible.status_code == 404

        missing_csrf = await alice_client.post("/api/v2/libraries", json={"name": "Research Group"})
        assert missing_csrf.status_code == 403
        csrf_headers = {"X-CSRF-Token": alice.csrf_token}
        created_group = await alice_client.post(
            "/api/v2/libraries",
            json={"name": "Research Group"},
            headers=csrf_headers,
        )
        assert created_group.status_code == 201
        group = created_group.json()
        assert group["role"] == "OWNER"

        invitation_response = await alice_client.post(
            f"/api/v2/libraries/{group['library_id']}/invitations",
            json={"email": f"{identity_prefix}-bob@example.test", "role": "EDITOR"},
            headers=csrf_headers,
        )
        assert invitation_response.status_code == 201
        invitation_token = invitation_response.json()["accept_token"]
        invitation_id = invitation_response.json()["invitation_id"]

        regenerated_response = await alice_client.post(
            f"/api/v2/libraries/{group['library_id']}/invitations/{invitation_id}/regenerate",
            headers=csrf_headers,
        )
        assert regenerated_response.status_code == 200
        regenerated_token = regenerated_response.json()["accept_token"]
        assert regenerated_token != invitation_token

        stale_accept = await bob_client.post(
            "/api/v2/library-invitations/accept",
            json={"token": invitation_token},
            headers={"X-CSRF-Token": bob.csrf_token},
        )
        assert stale_accept.status_code == 404

        bob_accept = await bob_client.post(
            "/api/v2/library-invitations/accept",
            json={"token": regenerated_token},
            headers={"X-CSRF-Token": bob.csrf_token},
        )
        assert bob_accept.status_code == 200
        assert bob_accept.json()["role"] == "EDITOR"

        bob_group = await bob_client.get(f"/api/v2/libraries/{group['library_id']}")
        assert bob_group.status_code == 200
        forbidden_owner_view = await bob_client.get(
            f"/api/v2/libraries/{group['library_id']}/invitations"
        )
        assert forbidden_owner_view.status_code == 404

        invalid_csrf = await bob_client.post(
            "/api/v2/library-invitations/accept",
            json={"token": invitation_token},
            headers={"X-CSRF-Token": f"wrong-{settings.csrf_cookie_name}"},
        )
        assert invalid_csrf.status_code == 403

        role_change = await alice_client.patch(
            f"/api/v2/libraries/{group['library_id']}/members/{bob.principal.principal_id}",
            json={"role": "VIEWER"},
            headers=csrf_headers,
        )
        assert role_change.status_code == 200
        assert role_change.json()["role"] == "VIEWER"

        removed = await alice_client.delete(
            f"/api/v2/libraries/{group['library_id']}/members/{bob.principal.principal_id}",
            headers=csrf_headers,
        )
        assert removed.status_code == 204
        assert (await bob_client.get(f"/api/v2/libraries/{group['library_id']}")).status_code == 404

        monkeypatch.setattr(
            OidcClient,
            "end_session_url",
            AsyncMock(return_value="http://identity.test/end-session"),
        )
        logout = await bob_client.post(
            "/api/v2/auth/logout",
            headers={
                "X-CSRF-Token": bob.csrf_token,
                "X-Forwarded-Host": "127.0.0.1:5174",
                "X-Forwarded-Proto": "http",
            },
        )
        assert logout.status_code == 200
        assert logout.json()["provider_logout_url"] == "http://identity.test/end-session"
        assert (await bob_client.get("/api/v2/auth/session")).status_code == 401

    async with migration_session_factory() as audit_session:
        event_types = set(
            await audit_session.scalars(
                select(AuditEvent.event_type).where(
                    (AuditEvent.actor_principal_id == alice.principal.principal_id)
                    | (AuditEvent.actor_principal_id == bob.principal.principal_id)
                )
            )
        )
        assert {
            "library.group_created",
            "library.invitation_created",
            "library.invitation_regenerated",
            "library.invitation_accepted",
            "library.membership_role_changed",
            "library.membership_revoked",
            "auth.session_revoked",
        } <= event_types


@pytest.mark.asyncio
async def test_m2_canonical_items_collections_and_local_overrides(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-m2-alice",
        email=f"{identity_prefix}-m2-alice@example.test",
        name="Alice M2",
    )
    bob = await provision_browser_session(
        subject=f"{identity_prefix}-m2-bob",
        email=f"{identity_prefix}-m2-bob@example.test",
        name="Bob M2",
    )
    async with authenticated_client(alice) as alice_client, authenticated_client(bob) as bob_client:
        alice_library = (await alice_client.get("/api/v2/libraries")).json()["libraries"][0]
        bob_library = (await bob_client.get("/api/v2/libraries")).json()["libraries"][0]
        alice_csrf = {"X-CSRF-Token": alice.csrf_token}
        bob_csrf = {"X-CSRF-Token": bob.csrf_token}

        root_response = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/collections",
            json={"name": "Review", "parent_collection_id": None},
            headers=alice_csrf,
        )
        assert root_response.status_code == 201
        root = root_response.json()
        second_response = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/collections",
            json={"name": "Methods", "parent_collection_id": root["collection_id"]},
            headers=alice_csrf,
        )
        assert second_response.status_code == 201
        second = second_response.json()
        tag_response = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/tags",
            json={"name": "Machine Learning", "color": "#356b55"},
            headers=alice_csrf,
        )
        assert tag_response.status_code == 201, tag_response.text
        tag = tag_response.json()

        doi = f"10.9999/{identity_prefix}"
        alice_item_response = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            json={
                "metadata": {
                    "title": "Canonical baseline title",
                    "publication_year": 2026,
                    "authors": [{"name": "A. Researcher"}],
                },
                "identifiers": [{"scheme": "DOI", "value": doi}],
                "collection_ids": [root["collection_id"]],
                "tag_ids": [tag["tag_id"]],
            },
            headers=alice_csrf,
        )
        assert alice_item_response.status_code == 201, alice_item_response.text
        alice_item = alice_item_response.json()

        added_again = await alice_client.put(
            f"/api/v2/libraries/{alice_library['library_id']}/collections/"
            f"{second['collection_id']}/items/{alice_item['library_item_id']}",
            headers=alice_csrf,
        )
        assert added_again.status_code == 204
        for collection in (root, second):
            result = await alice_client.get(
                f"/api/v2/libraries/{alice_library['library_id']}/items",
                params={"collection_id": collection["collection_id"]},
            )
            assert [value["library_item_id"] for value in result.json()["items"]] == [
                alice_item["library_item_id"]
            ]

        assert (
            await bob_client.get(
                f"/api/v2/libraries/{alice_library['library_id']}/items/"
                f"{alice_item['library_item_id']}"
            )
        ).status_code == 404

        bob_item_response = await bob_client.post(
            f"/api/v2/libraries/{bob_library['library_id']}/items",
            json={
                "metadata": {"title": "Attempted alternate canonical title"},
                "identifiers": [{"scheme": "DOI", "value": f"https://doi.org/{doi}"}],
            },
            headers=bob_csrf,
        )
        assert bob_item_response.status_code == 201, bob_item_response.text
        bob_item = bob_item_response.json()
        assert bob_item["canonical_paper_id"] == alice_item["canonical_paper_id"]
        assert bob_item["library_item_id"] != alice_item["library_item_id"]
        assert bob_item["effective_metadata"]["title"] == "Canonical baseline title"

        overridden_response = await alice_client.patch(
            f"/api/v2/libraries/{alice_library['library_id']}/items/"
            f"{alice_item['library_item_id']}/overrides",
            json={
                "expected_revision": alice_item["revision"],
                "overrides": {"title": "Alice local review title"},
            },
            headers=alice_csrf,
        )
        assert overridden_response.status_code == 200
        overridden = overridden_response.json()
        assert overridden["effective_metadata"]["title"] == "Alice local review title"
        bob_unchanged = await bob_client.get(
            f"/api/v2/libraries/{bob_library['library_id']}/items/{bob_item['library_item_id']}"
        )
        assert bob_unchanged.json()["effective_metadata"]["title"] == "Canonical baseline title"

        updated_item_response = await alice_client.patch(
            f"/api/v2/libraries/{alice_library['library_id']}/items/"
            f"{alice_item['library_item_id']}",
            json={
                "expected_revision": overridden["revision"],
                "overrides": {"venue": "Alice local venue"},
                "collection_ids": [second["collection_id"]],
                "tag_ids": [tag["tag_id"]],
            },
            headers=alice_csrf,
        )
        assert updated_item_response.status_code == 200, updated_item_response.text
        updated_item = updated_item_response.json()
        assert updated_item["effective_metadata"]["venue"] == "Alice local venue"
        assert updated_item["collection_ids"] == [second["collection_id"]]
        assert updated_item["tag_ids"] == [tag["tag_id"]]
        advanced_match = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params=[
                ("title", "local review"),
                ("author", "researcher"),
                ("identifier", identity_prefix),
                ("venue", "local venue"),
                ("year_from", "2026"),
                ("year_to", "2026"),
                ("collection_ids", root["collection_id"]),
                ("tag_ids", tag["tag_id"]),
                ("tag_mode", "ALL"),
                ("include_subcollections", "true"),
                ("metadata_source", "UNDEFINED"),
                ("has_pdf", "false"),
            ],
        )
        assert advanced_match.status_code == 200, advanced_match.text
        assert [value["library_item_id"] for value in advanced_match.json()["items"]] == [
            alice_item["library_item_id"]
        ]
        root_items = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"collection_id": root["collection_id"]},
        )
        assert root_items.json()["items"] == []

        conflict = await alice_client.patch(
            f"/api/v2/libraries/{alice_library['library_id']}/items/"
            f"{alice_item['library_item_id']}/overrides",
            json={"expected_revision": 1, "overrides": {"venue": "Conflict"}},
            headers=alice_csrf,
        )
        assert conflict.status_code == 409

        trashed_response = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/items/"
            f"{alice_item['library_item_id']}/trash",
            json={"expected_revision": updated_item["revision"]},
            headers=alice_csrf,
        )
        assert trashed_response.status_code == 200
        trashed = trashed_response.json()
        active = await alice_client.get(f"/api/v2/libraries/{alice_library['library_id']}/items")
        assert active.json()["items"] == []
        collections_after_trash = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/collections"
        )
        assert {
            value["collection_id"]: value["item_count"]
            for value in collections_after_trash.json()["collections"]
        } == {root["collection_id"]: 0, second["collection_id"]: 0}
        tags_after_trash = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/tags"
        )
        assert tags_after_trash.json()["tags"][0]["item_count"] == 0
        trash = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"status": "TRASHED"},
        )
        assert len(trash.json()["items"]) == 1
        restored = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/items/"
            f"{alice_item['library_item_id']}/restore",
            json={"expected_revision": trashed["revision"]},
            headers=alice_csrf,
        )
        assert restored.status_code == 200
        restored_item = restored.json()
        collections_after_restore = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/collections"
        )
        assert {
            value["collection_id"]: value["item_count"]
            for value in collections_after_restore.json()["collections"]
        } == {root["collection_id"]: 0, second["collection_id"]: 1}
        tags_after_restore = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/tags"
        )
        assert tags_after_restore.json()["tags"][0]["item_count"] == 1

        bulk_remove_tag = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/items/bulk-organize",
            json={
                "items": [
                    {
                        "library_item_id": restored_item["library_item_id"],
                        "expected_revision": restored_item["revision"],
                    }
                ],
                "action": "REMOVE_TAG",
                "target_id": tag["tag_id"],
            },
            headers=alice_csrf,
        )
        assert bulk_remove_tag.status_code == 200, bulk_remove_tag.text
        assert bulk_remove_tag.json()["updated"] == 1

        for index in range(2):
            response = await alice_client.post(
                f"/api/v2/libraries/{alice_library['library_id']}/items",
                json={"metadata": {"title": f"Cursor paper {index}"}},
                headers=alice_csrf,
            )
            assert response.status_code == 201, response.text
        first_page = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items", params={"limit": 2}
        )
        assert len(first_page.json()["items"]) == 2
        assert first_page.json()["next_cursor"]
        second_page = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"limit": 2, "cursor": first_page.json()["next_cursor"]},
        )
        first_ids = {value["library_item_id"] for value in first_page.json()["items"]}
        second_ids = {value["library_item_id"] for value in second_page.json()["items"]}
        assert first_ids.isdisjoint(second_ids)
        assert len(second_ids) == 1

        title_page = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"sort": "TITLE", "direction": "ASC", "limit": 2},
        )
        assert title_page.status_code == 200, title_page.text
        title_cursor = title_page.json()["next_cursor"]
        assert title_cursor
        title_page_2 = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={
                "sort": "TITLE",
                "direction": "ASC",
                "limit": 2,
                "cursor": title_cursor,
            },
        )
        assert title_page_2.status_code == 200, title_page_2.text
        sorted_ids = {
            value["library_item_id"]
            for value in title_page.json()["items"] + title_page_2.json()["items"]
        }
        assert len(sorted_ids) == 3

        local_title_search = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"q": "local review"},
        )
        assert [value["library_item_id"] for value in local_title_search.json()["items"]] == [
            alice_item["library_item_id"]
        ]
        cross_field_search = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"q": "researcher 2026"},
        )
        assert [value["library_item_id"] for value in cross_field_search.json()["items"]] == [
            alice_item["library_item_id"]
        ]
        identifier_search = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"q": identity_prefix},
        )
        assert [value["library_item_id"] for value in identifier_search.json()["items"]] == [
            alice_item["library_item_id"]
        ]
        no_match = await alice_client.get(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            params={"q": "definitely absent bibliographic value"},
        )
        assert no_match.json()["items"] == []


async def test_m3_job_leases_retry_and_idempotency(identity_prefix: str) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-jobs-alice",
        email=f"{identity_prefix}-jobs-alice@example.test",
        name="Alice Jobs",
    )
    actor = Actor(
        principal_id=alice.principal.principal_id,
        display_name=alice.principal.display_name,
        session_id=uuid.uuid4(),
    )
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
    library_id = uuid.UUID(library["library_id"])

    async with migration_session_factory() as session:
        job = await job_service.enqueue(
            session,
            actor,
            library_id,
            job_type="PDF_IMPORT",
            payload={"fixture": True},
            idempotency_key="same-upload",
            progress_total=3,
            max_attempts=2,
        )
        duplicate = await job_service.enqueue(
            session,
            actor,
            library_id,
            job_type="PDF_IMPORT",
            payload={"fixture": "ignored"},
            idempotency_key="same-upload",
        )
        assert duplicate.job_id == job.job_id
        await session.commit()

    async with migration_session_factory() as session:
        claimed = await job_service.claim(session, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        assert claimed.job_id == job.job_id
        await job_service.progress(
            session,
            job.job_id,
            worker_id="worker-a",
            current=1,
            total=3,
            message="Fingerprinting",
        )
        await job_service.fail(
            session,
            job.job_id,
            worker_id="worker-a",
            error={"code": "TEST_RETRY"},
            retry_delay_seconds=0,
        )
        await session.commit()

    async with migration_session_factory() as session:
        retried = await job_service.claim(session, worker_id="worker-b", lease_seconds=30)
        assert retried is not None
        assert retried.job_id == job.job_id
        assert retried.attempt_count == 2
        await job_service.succeed(
            session,
            job.job_id,
            worker_id="worker-b",
            result={"asset_id": str(uuid.uuid4())},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_m3_current_artifact_inheritance_and_explicit_selection(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-artifact-alice",
        email=f"{identity_prefix}-artifact-alice@example.test",
        name="Alice Artifact",
    )
    bob = await provision_browser_session(
        subject=f"{identity_prefix}-artifact-bob",
        email=f"{identity_prefix}-artifact-bob@example.test",
        name="Bob Artifact",
    )
    async with authenticated_client(alice) as alice_client, authenticated_client(bob) as bob_client:
        alice_library = (await alice_client.get("/api/v2/libraries")).json()["libraries"][0]
        bob_library = (await bob_client.get("/api/v2/libraries")).json()["libraries"][0]
        doi = f"10.9998/{identity_prefix}"
        alice_item_response = await alice_client.post(
            f"/api/v2/libraries/{alice_library['library_id']}/items",
            json={
                "metadata": {"title": "Shared artifact paper"},
                "identifiers": [{"scheme": "DOI", "value": doi}],
            },
            headers={"X-CSRF-Token": alice.csrf_token},
        )
        bob_item_response = await bob_client.post(
            f"/api/v2/libraries/{bob_library['library_id']}/items",
            json={
                "metadata": {"title": "Ignored duplicate title"},
                "identifiers": [{"scheme": "DOI", "value": doi}],
            },
            headers={"X-CSRF-Token": bob.csrf_token},
        )
        assert alice_item_response.status_code == 201
        assert bob_item_response.status_code == 201
        alice_item = alice_item_response.json()
        bob_item = bob_item_response.json()

    canonical_paper_id = uuid.UUID(alice_item["canonical_paper_id"])
    alice_library_id = uuid.UUID(alice_library["library_id"])
    bob_library_id = uuid.UUID(bob_library["library_id"])
    alice_item_id = uuid.UUID(alice_item["library_item_id"])
    bob_item_id = uuid.UUID(bob_item["library_item_id"])

    async with migration_session_factory() as session:
        canonical_v1 = Blob(
            sha256="1" * 64,
            byte_size=10,
            media_type="application/pdf",
            storage_bucket="test",
            storage_key=f"test/{identity_prefix}/canonical-v1",
            status="AVAILABLE",
            created_by=alice.principal.principal_id,
        )
        canonical_v2 = Blob(
            sha256="2" * 64,
            byte_size=11,
            media_type="application/pdf",
            storage_bucket="test",
            storage_key=f"test/{identity_prefix}/canonical-v2",
            status="AVAILABLE",
            created_by=alice.principal.principal_id,
        )
        alice_choice = Blob(
            sha256="3" * 64,
            byte_size=12,
            media_type="application/pdf",
            storage_bucket="test",
            storage_key=f"test/{identity_prefix}/alice-choice",
            status="AVAILABLE",
            created_by=alice.principal.principal_id,
        )
        derived = Blob(
            sha256="4" * 64,
            byte_size=20,
            media_type="text/markdown",
            storage_bucket="test",
            storage_key=f"test/{identity_prefix}/derived",
            status="AVAILABLE",
            created_by=alice.principal.principal_id,
        )
        session.add_all([canonical_v1, canonical_v2, alice_choice, derived])
        await session.flush()
        await artifact_service.set_canonical(
            session,
            canonical_paper_id=canonical_paper_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=canonical_v1.blob_id,
            media_type="application/pdf",
            actor_principal_id=alice.principal.principal_id,
        )
        await artifact_service.set_canonical(
            session,
            canonical_paper_id=canonical_paper_id,
            artifact_key="pipeline:summary",
            artifact_type="PIPELINE_DOCUMENT",
            blob_id=derived.blob_id,
            media_type="text/markdown",
            actor_principal_id=alice.principal.principal_id,
            source_fingerprint=canonical_v1.sha256,
        )
        await session.commit()

    async with migration_session_factory() as session:
        alice_effective = await artifact_service.resolve(
            session,
            library_id=alice_library_id,
            library_item_id=alice_item_id,
            artifact_key="pdf",
        )
        bob_effective = await artifact_service.resolve(
            session,
            library_id=bob_library_id,
            library_item_id=bob_item_id,
            artifact_key="pdf",
        )
        assert alice_effective is not None and alice_effective.blob_id == canonical_v1.blob_id
        assert bob_effective is not None and bob_effective.blob_id == canonical_v1.blob_id
        assert alice_effective.origin == bob_effective.origin == "CANONICAL"

        await artifact_service.specify_for_item(
            session,
            library_id=alice_library_id,
            library_item_id=alice_item_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=alice_choice.blob_id,
            media_type="application/pdf",
            actor_principal_id=alice.principal.principal_id,
        )
        await artifact_service.set_canonical(
            session,
            canonical_paper_id=canonical_paper_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=canonical_v2.blob_id,
            media_type="application/pdf",
            actor_principal_id=alice.principal.principal_id,
        )
        await session.commit()

    async with migration_session_factory() as session:
        alice_effective = await artifact_service.resolve(
            session,
            library_id=alice_library_id,
            library_item_id=alice_item_id,
            artifact_key="pdf",
        )
        bob_effective = await artifact_service.resolve(
            session,
            library_id=bob_library_id,
            library_item_id=bob_item_id,
            artifact_key="pdf",
        )
        assert alice_effective is not None and alice_effective.blob_id == alice_choice.blob_id
        assert alice_effective.origin == "OVERRIDE"
        assert bob_effective is not None and bob_effective.blob_id == canonical_v2.blob_id
        assert bob_effective.origin == "CANONICAL"
        derived_artifact = await session.scalar(
            select(Artifact).where(
                Artifact.canonical_paper_id == canonical_paper_id,
                Artifact.artifact_key == "pipeline:summary",
            )
        )
        assert derived_artifact is not None and derived_artifact.status == "STALE"

        assert await artifact_service.cancel_for_item(
            session,
            library_id=alice_library_id,
            library_item_id=alice_item_id,
            artifact_key="pdf",
        )
        await session.commit()

    async with migration_session_factory() as session:
        alice_effective = await artifact_service.resolve(
            session,
            library_id=alice_library_id,
            library_item_id=alice_item_id,
            artifact_key="pdf",
        )
        assert alice_effective is not None and alice_effective.blob_id == canonical_v2.blob_id
        assert alice_effective.origin == "CANONICAL"

    async with session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.principal_id', :principal_id, true)"),
            {"principal_id": str(bob.principal.principal_id)},
        )
        assert list(await session.scalars(select(ItemArtifactOverride))) == []


@pytest.mark.asyncio
async def test_m3_zotero_folder_upload_promotes_primary_pdf_and_keeps_extra_as_asset(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-zotero-folder-alice",
        email=f"{identity_prefix}-zotero-folder-alice@example.test",
        name="Alice Zotero Folder",
    )
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
        created = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/items",
                json={"metadata": {"title": "Zotero folder paper"}, "identifiers": []},
                headers={"X-CSRF-Token": alice.csrf_token},
            )
        ).json()

    library_id = uuid.UUID(library["library_id"])
    item_id = uuid.UUID(created["library_item_id"])
    async with migration_session_factory() as session:
        source = ZoteroImportSource(
            library_id=library_id,
            source_identity=f"zotero-user:{identity_prefix}",
            display_name="Alice Zotero",
            last_imported_at=datetime.now(UTC),
        )
        session.add(source)
        await session.flush()
        session.add(
            ZoteroImportEntry(
                source_id=source.source_id,
                zotero_library_id=1,
                item_key="PAPERKEY",
                library_id=library_id,
                library_item_id=item_id,
                item_version=1,
                item_type="journalArticle",
                attachment_manifest=[
                    {
                        "item_key": "PRIMARY1",
                        "path": "storage:paper.pdf",
                        "content_type": "application/pdf",
                        "link_mode": 0,
                        "file_available": False,
                    },
                    {
                        "item_key": "SUPPLEM1",
                        "path": "storage:supplement.pdf",
                        "content_type": "application/pdf",
                        "link_mode": 0,
                        "file_available": False,
                    },
                ],
            )
        )
        job = BackgroundJob(
            library_id=library_id,
            job_type="ZOTERO_IMPORT",
            status="SUCCEEDED",
            payload={},
            result={"source_id": str(source.source_id)},
            progress_current=1,
            progress_total=1,
            attempt_count=0,
            max_attempts=2,
            available_at=datetime.now(UTC),
            correlation_id=uuid.uuid4(),
            actor_principal_id=alice.principal.principal_id,
        )
        session.add(job)
        await session.commit()
        job_id = job.job_id

    pdf = b"%PDF-1.4\n% Zotero integration test\n%%EOF\n"
    async with authenticated_client(alice) as client:
        manifest_response = await client.get(
            f"/api/v2/libraries/{library_id}/imports/zotero/{job_id}/attachments"
        )
        assert manifest_response.status_code == 200
        declarations = manifest_response.json()["attachments"]
        assert [value["relative_path"] for value in declarations] == [
            "storage/PRIMARY1/paper.pdf",
            "storage/SUPPLEM1/supplement.pdf",
        ]
        unlinked = await client.get(f"/api/v2/libraries/{library_id}/items/{item_id}")
        assert unlinked.status_code == 200
        assert unlinked.json()["pdf_attachment"] is None
        assert unlinked.json()["asset_attachments"] == []
        for declaration in declarations:
            response = await client.post(
                f"/api/v2/libraries/{library_id}/imports/zotero/{job_id}/attachments/"
                f"{declaration['attachment_key']}",
                params={"zotero_library_id": 1, "item_key": "PAPERKEY"},
                content=pdf + declaration["attachment_key"].encode(),
                headers={
                    "X-CSRF-Token": alice.csrf_token,
                    "Content-Type": "application/pdf",
                },
            )
            assert response.status_code == 200, response.text
        refreshed = await client.get(f"/api/v2/libraries/{library_id}/items/{item_id}")
        assert refreshed.status_code == 200
        assert refreshed.json()["pdf_attachment"] == {
            "origin": "OVERRIDE",
            "artifact_type": "SOURCE_PDF",
            "filename": "paper.pdf",
            "media_type": "application/pdf",
            "revision": 1,
        }
        assert refreshed.json()["asset_attachments"] == [
            {
                "asset_id": refreshed.json()["asset_attachments"][0]["asset_id"],
                "filename": "supplement.pdf",
                "media_type": "application/pdf",
                "revision": 1,
            }
        ]

    async with migration_session_factory() as session:
        canonical = await session.scalar(
            select(Artifact).where(
                Artifact.canonical_paper_id == uuid.UUID(created["canonical_paper_id"]),
                Artifact.artifact_key == "pdf",
            )
        )
        override = await session.get(ItemArtifactOverride, (library_id, item_id, "pdf"))
        assets = list(
            await session.scalars(
                select(Asset).where(
                    Asset.library_id == library_id,
                    Asset.library_item_id == item_id,
                )
            )
        )
        assert canonical is not None and override is not None
        assert canonical.blob_id == override.blob_id
        assert canonical.provenance["verification_status"] == "UNVERIFIED"
        assert len(assets) == 1
        assert assets[0].provenance["zotero_attachment_key"] == "SUPPLEM1"


@pytest.mark.asyncio
async def test_item_resource_api_manages_pdf_override_assets_and_content_access(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-resources-alice",
        email=f"{identity_prefix}-resources-alice@example.test",
        name="Alice Resources",
    )
    bob = await provision_browser_session(
        subject=f"{identity_prefix}-resources-bob",
        email=f"{identity_prefix}-resources-bob@example.test",
        name="Bob Resources",
    )
    csrf = {"X-CSRF-Token": alice.csrf_token}
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
        item = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/items",
                json={"metadata": {"title": "Resource API paper"}, "identifiers": []},
                headers=csrf,
            )
        ).json()
        resource_path = (
            f"/api/v2/libraries/{library['library_id']}/items/{item['library_item_id']}/resources"
        )
        empty = await client.get(resource_path)
        assert empty.status_code == 200
        assert empty.json()["primary_pdf"] is None
        assert empty.json()["documents"] == []
        assert empty.json()["assets"] == []

        uploaded_asset = await client.post(
            f"{resource_path}/assets",
            params={"filename": "reading-notes.md"},
            content=b"# Reading notes\n",
            headers={**csrf, "Content-Type": "text/markdown"},
        )
        assert uploaded_asset.status_code == 201
        asset = uploaded_asset.json()
        assert asset["filename"] == "reading-notes.md"
        assert asset["media_type"] == "text/markdown"
        after_asset = await client.get(resource_path)
        assert after_asset.status_code == 200, after_asset.text

        pdf = await client.put(
            f"{resource_path}/primary-pdf",
            params={"filename": "paper.pdf"},
            content=b"%PDF-1.4\nresource test\n%%EOF\n",
            headers={**csrf, "Content-Type": "application/pdf"},
        )
        assert pdf.status_code == 200, pdf.text
        assert pdf.json()["canonical_promoted"] is True
        assert pdf.json()["primary_pdf"]["origin"] == "OVERRIDE"

        async with migration_session_factory() as session:
            canonical_pdf = await session.scalar(
                select(Artifact).where(
                    Artifact.canonical_paper_id == uuid.UUID(item["canonical_paper_id"]),
                    Artifact.artifact_key == "pdf",
                )
            )
            assert canonical_pdf is not None
            session.add(
                Artifact(
                    canonical_paper_id=uuid.UUID(item["canonical_paper_id"]),
                    artifact_key="document:test-summary",
                    artifact_type="PIPELINE_DOCUMENT",
                    blob_id=canonical_pdf.blob_id,
                    status="ACTIVE",
                    original_filename="summary.md",
                    media_type="text/markdown",
                    provenance={"source": "test_pipeline"},
                    revision=1,
                    updated_by=alice.principal.principal_id,
                )
            )
            await session.commit()

        catalogue = (await client.get(resource_path)).json()
        assert catalogue["primary_pdf"]["filename"] == "paper.pdf"
        assert [value["filename"] for value in catalogue["documents"]] == ["summary.md"]
        assert [value["filename"] for value in catalogue["assets"]] == ["reading-notes.md"]
        item_summary = (
            await client.get(
                f"/api/v2/libraries/{library['library_id']}/items/{item['library_item_id']}"
            )
        ).json()["resource_summary"]
        assert item_summary == {
            "primary_pdf": 1,
            "extracted_text": 0,
            "documents": 1,
            "assets": 1,
        }

        opened_pdf = await client.get(f"{resource_path}/artifacts/pdf/content")
        assert opened_pdf.status_code == 307
        assert opened_pdf.headers["location"].startswith("http://127.0.0.1:9002/")
        opened_legacy_document = await client.get(
            f"{resource_path}/artifacts/document:test-summary/content"
        )
        assert opened_legacy_document.status_code == 307
        opened_asset = await client.get(f"{resource_path}/assets/{asset['asset_id']}/content")
        assert opened_asset.status_code == 307
        assert opened_asset.headers["location"].startswith("http://127.0.0.1:9002/")

        renamed = await client.patch(
            f"{resource_path}/assets/{asset['asset_id']}",
            json={"display_name": "notes-renamed.md", "expected_revision": 1},
            headers=csrf,
        )
        assert renamed.status_code == 200
        assert renamed.json()["revision"] == 2
        assert renamed.json()["filename"] == "notes-renamed.md"

        cancelled = await client.delete(
            f"{resource_path}/primary-pdf",
            params={"expected_revision": 1},
            headers=csrf,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["primary_pdf"]["origin"] == "CANONICAL"

        deleted = await client.delete(
            f"{resource_path}/assets/{asset['asset_id']}",
            params={"expected_revision": 2},
            headers=csrf,
        )
        assert deleted.status_code == 204
        final_catalogue = (await client.get(resource_path)).json()
        assert final_catalogue["assets"] == []

    async with authenticated_client(bob) as client:
        hidden = await client.get(resource_path)
        assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_m3_metadata_provider_prefers_crossref_and_falls_back_to_openalex() -> None:
    calls: list[str] = []

    async def crossref_hit(request: Request) -> Response:
        calls.append(request.url.host or "")
        return Response(
            200,
            json={
                "message": {
                    "DOI": "10.1000/example",
                    "URL": "https://doi.org/10.1000/example",
                    "title": ["Crossref title"],
                    "container-title": ["Crossref Journal"],
                    "published": {"date-parts": [[2025, 4, 2]]},
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                }
            },
        )

    async with AsyncClient(transport=MockTransport(crossref_hit)) as client:
        resolver = DoiMetadataResolver(
            crossref_base_url="https://api.crossref.org",
            openalex_base_url="https://api.openalex.org",
            timeout_seconds=5,
            mailto=None,
            client=client,
        )
        result = await resolver.resolve("https://doi.org/10.1000/EXAMPLE")
    assert result is not None and result.source == "CROSSREF"
    assert result.title == "Crossref title"
    assert result.publication_date == date(2025, 4, 2)
    assert result.publication_date_precision == "DAY"
    assert calls == ["api.crossref.org"]

    year_only = DoiMetadataResolver._from_crossref(
        {
            "DOI": "10.1000/year-only",
            "title": ["Year-only record"],
            "published": {"date-parts": [[2024]]},
        },
        "10.1000/year-only",
    )
    assert year_only is not None
    assert year_only.publication_year == 2024
    assert year_only.publication_month is None
    assert year_only.publication_day is None
    assert year_only.publication_date is None
    assert year_only.publication_date_precision == "YEAR"

    calls.clear()

    async def crossref_miss_openalex_hit(request: Request) -> Response:
        calls.append(request.url.host or "")
        if request.url.host == "api.crossref.org":
            raise ConnectTimeout("Crossref timed out", request=request)
        return Response(
            200,
            json={
                "id": "https://openalex.org/W123",
                "doi": "https://doi.org/10.1000/example",
                "title": "OpenAlex title",
                "publication_year": 2024,
                "publication_date": "2024-03-01",
                "authorships": [
                    {
                        "author": {"display_name": "Grace Hopper", "id": "A123"},
                        "is_corresponding": True,
                    }
                ],
                "primary_location": {"source": {"display_name": "OpenAlex Venue"}},
            },
        )

    async with AsyncClient(transport=MockTransport(crossref_miss_openalex_hit)) as client:
        resolver = DoiMetadataResolver(
            crossref_base_url="https://api.crossref.org",
            openalex_base_url="https://api.openalex.org",
            timeout_seconds=5,
            mailto=None,
            client=client,
        )
        result = await resolver.resolve("10.1000/example")
    assert result is not None and result.source == "OPENALEX"
    assert result.title == "OpenAlex title"
    assert calls == ["api.crossref.org", "api.openalex.org"]


@pytest.mark.asyncio
async def test_m3_arxiv_provider_uses_atom_metadata_and_published_doi() -> None:
    calls: list[str] = []
    atom = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>https://arxiv.org/abs/2501.01234v2</id>
        <published>2025-01-03T00:00:00Z</published>
        <title>  An arXiv   title </title>
        <summary>An abstract.</summary>
        <author><name>Ada Lovelace</name></author>
        <category term="cs.AI" />
        <arxiv:doi>10.1000/published</arxiv:doi>
      </entry>
    </feed>"""

    async def handler(request: Request) -> Response:
        calls.append(request.url.host or "")
        if request.url.host == "export.arxiv.org":
            return Response(200, text=atom)
        if request.url.host == "api.crossref.org":
            return Response(
                200,
                json={
                    "message": {
                        "DOI": "10.1000/published",
                        "title": ["Published title"],
                        "published": {"date-parts": [[2026, 2, 4]]},
                    }
                },
            )
        return Response(404)

    async with AsyncClient(transport=MockTransport(handler)) as client:
        resolver = DoiMetadataResolver(
            crossref_base_url="https://api.crossref.org",
            openalex_base_url="https://api.openalex.org",
            arxiv_api_base_url="https://export.arxiv.org/api",
            arxiv_min_interval_seconds=0,
            timeout_seconds=5,
            mailto=None,
            client=client,
        )
        result = await resolver.resolve("10.48550/arXiv.2501.01234")

    assert result is not None and result.source == "CROSSREF"
    assert result.doi == "10.1000/published"
    assert ("ARXIV", "2501.01234") in result.related_identifiers
    assert calls == ["export.arxiv.org", "api.crossref.org"]


@pytest.mark.asyncio
async def test_m3_arxiv_provider_falls_back_to_exact_openalex_doi() -> None:
    calls: list[str] = []

    async def handler(request: Request) -> Response:
        calls.append(request.url.host or "")
        if request.url.host == "export.arxiv.org":
            return Response(429, headers={"Retry-After": "0"})
        return Response(
            200,
            json={
                "id": "https://openalex.org/W123",
                "title": "OpenAlex arXiv title",
                "publication_year": 2025,
                "publication_date": "2025-01-03",
            },
        )

    async with AsyncClient(transport=MockTransport(handler)) as client:
        resolver = DoiMetadataResolver(
            crossref_base_url="https://api.crossref.org",
            openalex_base_url="https://api.openalex.org",
            arxiv_api_base_url="https://export.arxiv.org/api",
            arxiv_min_interval_seconds=0,
            timeout_seconds=5,
            mailto=None,
            client=client,
        )
        result = await resolver.resolve("2501.01234", scheme="ARXIV")

    assert result is not None and result.source == "OPENALEX"
    assert ("ARXIV", "2501.01234") in result.related_identifiers
    assert calls == ["export.arxiv.org", "export.arxiv.org", "api.openalex.org"]


class FakeMetadataResolver:
    def __init__(self, result: ResolvedMetadata | None) -> None:
        self.result = result

    async def resolve(
        self, doi: str, *, scheme: Literal["DOI", "ARXIV"] = "DOI"
    ) -> ResolvedMetadata | None:
        return self.result


@pytest.mark.asyncio
async def test_m3_refresh_overwrites_only_on_success_and_stops_after_two_failures(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-refresh-alice",
        email=f"{identity_prefix}-refresh-alice@example.test",
        name="Alice Refresh",
    )
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
        csrf = {"X-CSRF-Token": alice.csrf_token}
        created_response = await client.post(
            f"/api/v2/libraries/{library['library_id']}/items",
            json={
                "metadata": {"title": "Limited extracted title"},
                "identifiers": [{"scheme": "DOI", "value": "10.1000/refresh-test"}],
            },
            headers=csrf,
        )
        assert created_response.status_code == 201
        created = created_response.json()
        assert created["metadata_source"] == "UNDEFINED"

        request_id = uuid.uuid4()
        enqueue_response = await client.post(
            f"/api/v2/libraries/{library['library_id']}/items/metadata-refresh",
            json={
                "library_item_ids": [created["library_item_id"]],
                "request_id": str(request_id),
                "refresh_mode": "MANUAL",
            },
            headers=csrf,
        )
        assert enqueue_response.status_code == 202
        success_job_id = uuid.UUID(enqueue_response.json()["jobs"][0]["job_id"])

    resolved = ResolvedMetadata(
        source="CROSSREF",
        source_record_id="https://doi.org/10.1000/refresh-test",
        doi="10.1000/refresh-test",
        title="Authoritative Crossref title",
        abstract="Complete abstract",
        publication_year=2025,
        publication_month=2,
        publication_day=3,
        publication_date=date(2025, 2, 3),
        publication_date_precision="DAY",
        work_type="JOURNAL_ARTICLE",
        venue="Journal of Tests",
        canonical_url="https://doi.org/10.1000/refresh-test",
        publisher="Test Publisher",
        volume="12",
        issue="3",
        pages="100-110",
        article_number=None,
        language="en",
        issn=["1234-5678"],
        isbn=[],
        authors=[{"name": "Test Author"}],
        extra={"type": "journal-article"},
    )
    async with worker_session_factory() as session:
        job = await job_service.claim(
            session, worker_id="metadata-worker", job_types={METADATA_REFRESH_JOB}
        )
        assert job is not None and job.job_id == success_job_id
        await metadata_refresh_service.execute_claimed(
            session,
            job,
            worker_id="metadata-worker",
            resolver=FakeMetadataResolver(resolved),
        )
        await session.commit()

    async with authenticated_client(alice) as client:
        refreshed = await client.get(
            f"/api/v2/libraries/{library['library_id']}/items/{created['library_item_id']}"
        )
        assert refreshed.status_code == 200
        refreshed_item = refreshed.json()
        assert refreshed_item["metadata_source"] == "CROSSREF"
        assert refreshed_item["canonical_metadata"]["title"] == "Authoritative Crossref title"
        revision_after_success = refreshed_item["metadata_revision"]

        failed_enqueue = await client.post(
            f"/api/v2/libraries/{library['library_id']}/items/metadata-refresh",
            json={
                "library_item_ids": [created["library_item_id"]],
                "request_id": str(uuid.uuid4()),
                "refresh_mode": "MANUAL",
            },
            headers={"X-CSRF-Token": alice.csrf_token},
        )
        failed_job_id = uuid.UUID(failed_enqueue.json()["jobs"][0]["job_id"])

    for attempt in range(2):
        async with worker_session_factory() as session:
            job = await job_service.claim(
                session,
                worker_id=f"metadata-worker-{attempt}",
                job_types={METADATA_REFRESH_JOB},
            )
            assert job is not None and job.job_id == failed_job_id
            await metadata_refresh_service.execute_claimed(
                session,
                job,
                worker_id=f"metadata-worker-{attempt}",
                resolver=FakeMetadataResolver(None),
            )
            await session.commit()

    async with authenticated_client(alice) as client:
        unchanged = (
            await client.get(
                f"/api/v2/libraries/{library['library_id']}/items/{created['library_item_id']}"
            )
        ).json()
        assert unchanged["metadata_source"] == "CROSSREF"
        assert unchanged["canonical_metadata"]["title"] == "Authoritative Crossref title"
        assert unchanged["metadata_revision"] == revision_after_success
        failed_job = await client.get(
            f"/api/v2/libraries/{library['library_id']}/jobs/{failed_job_id}"
        )
        assert failed_job.json()["status"] == "FAILED"
        assert failed_job.json()["attempt_count"] == 2


@pytest.mark.asyncio
async def test_m3_doi_reconciliation_reuses_resolved_canonical_and_merges_items(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-reconcile-alice",
        email=f"{identity_prefix}-reconcile-alice@example.test",
        name="Alice Reconcile",
    )
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
        csrf = {"X-CSRF-Token": alice.csrf_token}
        first_collection = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/collections",
                json={"name": "Known", "parent_collection_id": None},
                headers=csrf,
            )
        ).json()
        second_collection = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/collections",
                json={"name": "Imported", "parent_collection_id": None},
                headers=csrf,
            )
        ).json()
        target = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/items",
                json={
                    "metadata": {"title": "Resolved target"},
                    "identifiers": [{"scheme": "DOI", "value": "10.1000/reconcile"}],
                    "collection_ids": [first_collection["collection_id"]],
                },
                headers=csrf,
            )
        ).json()
        provisional = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/items",
                json={
                    "metadata": {"title": "Title extracted from a file"},
                    "collection_ids": [second_collection["collection_id"]],
                },
                headers=csrf,
            )
        ).json()

    async with migration_session_factory() as session:
        metadata = await session.get(CanonicalMetadata, uuid.UUID(target["canonical_paper_id"]))
        assert metadata is not None
        metadata.metadata_source = "CROSSREF"
        metadata.source_record_id = "https://doi.org/10.1000/reconcile"
        await session.commit()

    async with migration_session_factory() as session:
        result = await doi_reconciliation_service.reconcile(
            session,
            library_id=uuid.UUID(library["library_id"]),
            library_item_id=uuid.UUID(provisional["library_item_id"]),
            doi="https://doi.org/10.1000/RECONCILE",
            actor_principal_id=alice.principal.principal_id,
        )
        assert result.merged_item
        assert result.library_item_id == uuid.UUID(target["library_item_id"])
        assert result.canonical_paper_id == uuid.UUID(target["canonical_paper_id"])
        assert result.metadata_already_resolved
        await session.commit()

    async with authenticated_client(alice) as client:
        merged = await client.get(
            f"/api/v2/libraries/{library['library_id']}/items/{target['library_item_id']}"
        )
        assert merged.status_code == 200
        assert set(merged.json()["collection_ids"]) == {
            first_collection["collection_id"],
            second_collection["collection_id"],
        }
        removed = await client.get(
            f"/api/v2/libraries/{library['library_id']}/items/{provisional['library_item_id']}"
        )
        assert removed.status_code == 404


class FakeCitationStorage:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read_bytes(self, key: str, max_bytes: int) -> bytes:
        assert key == "test/citations.bib"
        assert len(self.data) <= max_bytes
        return self.data


class FakePdfStorage:
    async def read_bytes(self, key: str, max_bytes: int) -> bytes:
        assert key == "test/paper.pdf"
        return b"%PDF-fake"


class FakePdfExtractor:
    async def extract(self, data: bytes) -> PdfText:
        assert data == b"%PDF-fake"
        return PdfText(
            text="Published as doi:10.1000/PDF-MERGE; arXiv:2401.12345v2",
            page_count=8,
            pages_examined=8,
        )


@pytest.mark.asyncio
async def test_m3_citation_handler_initializes_limited_items_and_refresh_jobs(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-citation-alice",
        email=f"{identity_prefix}-citation-alice@example.test",
        name="Alice Citation",
    )
    actor = Actor(
        principal_id=alice.principal.principal_id,
        display_name=alice.principal.display_name,
        session_id=uuid.uuid4(),
    )
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
    library_id = uuid.UUID(library["library_id"])
    citation_data = b"""@article{with-doi,
      title={Imported With DOI}, year={2025}, doi={10.1000/IMPORT}
    }
    @article{without-doi,
      title={Imported Without DOI}, year={2024}
    }"""

    async with migration_session_factory() as session:
        blob = Blob(
            sha256="5" * 64,
            byte_size=len(citation_data),
            media_type="application/x-bibtex",
            storage_bucket="test",
            storage_key="test/citations.bib",
            status="AVAILABLE",
            created_by=alice.principal.principal_id,
        )
        session.add(blob)
        await session.flush()
        import_job = await job_service.enqueue(
            session,
            actor,
            library_id,
            job_type=CITATION_IMPORT_JOB,
            payload={"blob_id": str(blob.blob_id), "filename": "citations.bib"},
            idempotency_key=f"citation-test:{identity_prefix}",
            max_attempts=2,
        )
        await session.commit()

    async with worker_session_factory() as session:
        claimed = await job_service.claim(
            session,
            worker_id="citation-worker",
            job_types={CITATION_IMPORT_JOB},
        )
        assert claimed is not None and claimed.job_id == import_job.job_id
        await CitationImportHandler(
            max_bytes=1024 * 1024,
            storage=FakeCitationStorage(citation_data),  # type: ignore[arg-type]
        ).handle(session, claimed, worker_id="citation-worker")
        await session.commit()

    async with authenticated_client(alice) as client:
        items = (await client.get(f"/api/v2/libraries/{library_id}/items")).json()["items"]
        imported = {
            value["canonical_metadata"]["title"]: value
            for value in items
            if value["canonical_metadata"]["title"].startswith("Imported")
        }
        assert set(imported) == {"Imported With DOI", "Imported Without DOI"}
        assert imported["Imported With DOI"]["metadata_source"] == "UNDEFINED"
        assert imported["Imported Without DOI"]["identifiers"] == []

    async with migration_session_factory() as session:
        completed = await session.get(BackgroundJob, import_job.job_id)
        assert completed is not None and completed.status == "SUCCEEDED"
        assert completed.result is not None
        assert completed.result["record_count"] == 2
        assert completed.result["metadata_refresh_jobs"] == 1


@pytest.mark.asyncio
async def test_m3_pdf_handler_merges_provisional_item_and_preserves_pdf_override(
    identity_prefix: str,
) -> None:
    alice = await provision_browser_session(
        subject=f"{identity_prefix}-pdf-alice",
        email=f"{identity_prefix}-pdf-alice@example.test",
        name="Alice PDF",
    )
    actor = Actor(
        principal_id=alice.principal.principal_id,
        display_name=alice.principal.display_name,
        session_id=uuid.uuid4(),
    )
    async with authenticated_client(alice) as client:
        library = (await client.get("/api/v2/libraries")).json()["libraries"][0]
        csrf = {"X-CSRF-Token": alice.csrf_token}
        target = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/items",
                json={
                    "metadata": {"title": "Resolved target"},
                    "identifiers": [{"scheme": "DOI", "value": "10.1000/pdf-merge"}],
                },
                headers=csrf,
            )
        ).json()
        provisional = (
            await client.post(
                f"/api/v2/libraries/{library['library_id']}/items",
                json={"metadata": {"title": "uploaded-file"}, "identifiers": []},
                headers=csrf,
            )
        ).json()

    library_id = uuid.UUID(library["library_id"])
    target_id = uuid.UUID(target["library_item_id"])
    provisional_id = uuid.UUID(provisional["library_item_id"])
    async with migration_session_factory() as session:
        metadata = await session.get(
            CanonicalMetadata,
            uuid.UUID(target["canonical_paper_id"]),
        )
        assert metadata is not None
        metadata.metadata_source = "CROSSREF"
        metadata.source_record_id = "https://doi.org/10.1000/pdf-merge"
        blob = Blob(
            sha256="6" * 64,
            byte_size=9,
            media_type="application/pdf",
            storage_bucket="test",
            storage_key="test/paper.pdf",
            status="AVAILABLE",
            created_by=alice.principal.principal_id,
        )
        session.add(blob)
        await session.flush()
        await artifact_service.specify_for_item(
            session,
            library_id=library_id,
            library_item_id=provisional_id,
            artifact_key="pdf",
            artifact_type="SOURCE_PDF",
            blob_id=blob.blob_id,
            media_type="application/pdf",
            actor_principal_id=alice.principal.principal_id,
            original_filename="uploaded-file.pdf",
        )
        import_job = await job_service.enqueue(
            session,
            actor,
            library_id,
            job_type=PDF_IMPORT_JOB,
            payload={
                "blob_id": str(blob.blob_id),
                "library_item_id": str(provisional_id),
                "filename": "uploaded-file.pdf",
            },
            idempotency_key=f"pdf-test:{identity_prefix}",
            progress_total=3,
            max_attempts=2,
        )
        await session.commit()

    async with worker_session_factory() as session:
        claimed = await job_service.claim(
            session,
            worker_id="pdf-worker",
            job_types={PDF_IMPORT_JOB},
        )
        assert claimed is not None and claimed.job_id == import_job.job_id
        await PdfImportHandler(
            max_bytes=1024,
            storage=FakePdfStorage(),  # type: ignore[arg-type]
            extractor=FakePdfExtractor(),
        ).handle(session, claimed, worker_id="pdf-worker")
        await session.commit()

    async with migration_session_factory() as session:
        completed = await session.get(BackgroundJob, import_job.job_id)
        assert completed is not None and completed.status == "SUCCEEDED"
        assert completed.result is not None
        assert completed.result["library_item_id"] == str(target_id)
        assert completed.result["outcome"] == "READY"
        assert completed.result["merged_item"] is True
        assert completed.result["identifiers"] == [
            {"scheme": "DOI", "value": "10.1000/pdf-merge"},
            {"scheme": "ARXIV", "value": "2401.12345"},
        ]
        assert await session.get(LibraryItem, provisional_id) is None
        identifiers = set(
            (
                await session.execute(
                    select(
                        CanonicalIdentifier.scheme,
                        CanonicalIdentifier.normalized_value,
                    ).where(
                        CanonicalIdentifier.canonical_paper_id
                        == uuid.UUID(target["canonical_paper_id"])
                    )
                )
            ).tuples()
        )
        assert identifiers == {
            ("DOI", "10.1000/pdf-merge"),
            ("ARXIV", "2401.12345"),
        }
        override = await session.get(
            ItemArtifactOverride,
            (library_id, target_id, "pdf"),
        )
        assert override is not None and override.blob_id == blob.blob_id
