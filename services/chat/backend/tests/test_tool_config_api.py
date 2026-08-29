from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.app.database import api_session_factory
from backend.app.identity import Actor, require_admin
from backend.app.main import app
from backend.app.models import ToolRuntimeConfig


async def test_admin_can_update_server_controlled_tool_parameters() -> None:
    admin_id = uuid.uuid4()

    async def fake_admin() -> Actor:
        return Actor(principal_id=admin_id)

    async with api_session_factory() as session, session.begin():
        await session.execute(delete(ToolRuntimeConfig))
    app.dependency_overrides[require_admin] = fake_admin
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            initial = await client.get("/api/chat/v1/admin/tool-config")
            assert initial.status_code == 200
            assert initial.json()["tool_config"]["revision"] == 0
            updated = await client.patch(
                "/api/chat/v1/admin/tool-config",
                json={
                    "expected_revision": 0,
                    "retrieval_mode": "BM25",
                    "retrieval_top_k": 7,
                    "chunk_top_k_per_document": 3,
                    "doi_document_max_chars": 12_000,
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["tool_config"] == {
                "retrieval_mode": "BM25",
                "retrieval_top_k": 7,
                "chunk_top_k_per_document": 3,
                "doi_document_max_chars": 12_000,
                "revision": 1,
            }
            stale = await client.patch(
                "/api/chat/v1/admin/tool-config",
                json={
                    "expected_revision": 0,
                    "retrieval_mode": "HYBRID",
                    "retrieval_top_k": 20,
                    "chunk_top_k_per_document": 5,
                    "doi_document_max_chars": 20_000,
                },
            )
            assert stale.status_code == 422
    finally:
        app.dependency_overrides.pop(require_admin, None)
        async with api_session_factory() as session, session.begin():
            await session.execute(delete(ToolRuntimeConfig))
