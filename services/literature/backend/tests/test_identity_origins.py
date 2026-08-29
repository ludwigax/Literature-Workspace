from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.app.config import get_settings
from backend.app.identity.oidc import OidcClient
from backend.app.identity.origins import BrowserOrigin, browser_origin


def request(*, host: str, proto: str = "http") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v2/auth/login",
            "headers": [
                (b"host", b"api:8020"),
                (b"x-forwarded-host", host.encode()),
                (b"x-forwarded-proto", proto.encode()),
            ],
            "server": ("api", 8020),
        }
    )


def dynamic_settings():
    return get_settings().model_copy(
        update={
            "browser_origin_map": (
                '{"http://localhost:5174":"http://localhost:8081",'
                '"https://research.example":"https://auth.example"}'
            )
        }
    )


def test_browser_origin_selects_matching_keycloak_origin() -> None:
    local = browser_origin(request(host="localhost:5174"), dynamic_settings())
    assert local.app_origin == "http://localhost:5174"
    assert local.keycloak_origin == "http://localhost:8081"
    assert local.callback_url == "http://localhost:5174/api/v2/auth/callback"
    assert local.issuer == "http://localhost:8081/realms/literature-v2"

    public = browser_origin(
        request(host="research.example", proto="https"), dynamic_settings()
    )
    assert public.app_origin == "https://research.example"
    assert public.keycloak_origin == "https://auth.example"


def test_browser_origin_rejects_unconfigured_host() -> None:
    with pytest.raises(HTTPException) as raised:
        browser_origin(request(host="attacker.example"), dynamic_settings())
    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_oidc_browser_urls_stay_on_selected_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = dynamic_settings()
    client = OidcClient(settings)
    browser = BrowserOrigin(
        app_origin="http://localhost:5174",
        keycloak_origin="http://localhost:8081",
    )
    metadata = {
        "authorization_endpoint": (
            "http://localhost:8081/realms/literature-v2/protocol/openid-connect/auth"
        ),
        "end_session_endpoint": (
            "http://localhost:8081/realms/literature-v2/protocol/openid-connect/logout"
        ),
    }
    monkeypatch.setattr(client, "_metadata", AsyncMock(return_value=metadata))

    authorization = await client.authorization_url(
        browser=browser,
        state="state",
        nonce="nonce",
        code_challenge="challenge",
    )
    query = parse_qs(urlsplit(authorization).query)
    assert query["redirect_uri"] == ["http://localhost:5174/api/v2/auth/callback"]

    logout = await client.end_session_url(browser)
    assert logout is not None
    assert parse_qs(urlsplit(logout).query)["post_logout_redirect_uri"] == [
        "http://localhost:5174"
    ]
