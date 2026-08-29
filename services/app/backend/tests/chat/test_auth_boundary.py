from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from backend.app.authorization.dependencies import current_actor, require_csrf
from backend.app.config import get_settings
from backend.app.database import session_factory
from backend.app.identity.security import hash_token
from backend.app.main import app
from backend.app.models import Principal, WebSession


async def test_chat_uses_shared_web_session_and_csrf_not_legacy_identity_header() -> None:
    test_actor_override = app.dependency_overrides.pop(current_actor)
    test_csrf_override = app.dependency_overrides.pop(require_csrf)
    settings = get_settings()
    principal_id = uuid.uuid4()
    raw_session_token = f"session-{uuid.uuid4()}"
    raw_csrf_token = f"csrf-{uuid.uuid4()}"
    async with session_factory() as session, session.begin():
        session.add(
            Principal(
                principal_id=principal_id,
                display_name="Shared auth test",
                status="ACTIVE",
            )
        )
        session.add(
            WebSession(
                principal_id=principal_id,
                token_hash=hash_token(raw_session_token),
                csrf_token_hash=hash_token(raw_csrf_token),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            forged = await client.post(
                "/api/chat/v1/sessions",
                json={"title": "Forged"},
                headers={"X-Chat-Principal-Id": str(principal_id)},
            )
            assert forged.status_code == 401

            client.cookies.set(settings.session_cookie_name, raw_session_token)
            client.cookies.set(settings.csrf_cookie_name, raw_csrf_token)
            missing_csrf = await client.post(
                "/api/chat/v1/sessions", json={"title": "Missing CSRF"}
            )
            assert missing_csrf.status_code == 403

            created = await client.post(
                "/api/chat/v1/sessions",
                json={"title": "Shared identity"},
                headers={"X-CSRF-Token": raw_csrf_token},
            )
            assert created.status_code == 201, created.text
            assert created.json()["session"]["owner_principal_id"] == str(principal_id)
    finally:
        app.dependency_overrides[current_actor] = test_actor_override
        app.dependency_overrides[require_csrf] = test_csrf_override
