from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from ..config import Settings


@dataclass(frozen=True)
class BrowserOrigin:
    app_origin: str
    keycloak_origin: str

    @property
    def issuer(self) -> str:
        return f"{self.keycloak_origin}/realms/literature-v2"

    @property
    def callback_url(self) -> str:
        return f"{self.app_origin}/api/v2/auth/callback"


def browser_origin(request: Request, settings: Settings) -> BrowserOrigin:
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    host = forwarded_host or request.headers.get("host", "").strip()
    scheme = forwarded_proto or request.url.scheme
    origin = _normalize_origin(f"{scheme}://{host}")
    mapping = browser_origin_mapping(settings)
    keycloak_origin = mapping.get(origin)
    if keycloak_origin is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request origin is not configured for login",
        )
    return BrowserOrigin(app_origin=origin, keycloak_origin=keycloak_origin)


def browser_origin_mapping(settings: Settings) -> dict[str, str]:
    values: dict[str, str] = {}
    raw = settings.browser_origin_map.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("LITV2_BROWSER_ORIGIN_MAP must be a JSON object") from error
        if not isinstance(parsed, dict):
            raise RuntimeError("LITV2_BROWSER_ORIGIN_MAP must be a JSON object")
        for app_origin, keycloak_origin in parsed.items():
            values[_normalize_origin(str(app_origin))] = _normalize_origin(str(keycloak_origin))
    if not values:
        issuer = settings.oidc_issuer.rstrip("/")
        suffix = "/realms/literature-v2"
        keycloak_origin = issuer[: -len(suffix)] if issuer.endswith(suffix) else issuer
        values[_normalize_origin(settings.frontend_url)] = _normalize_origin(keycloak_origin)
        values[_normalize_origin(settings.public_api_base_url)] = _normalize_origin(
            keycloak_origin
        )
    return values


def _normalize_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"invalid browser origin: {value!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise RuntimeError(f"browser origin must contain only scheme and authority: {value!r}")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
