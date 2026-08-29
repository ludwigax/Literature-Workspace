from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet

from ..config import Settings
from .origins import BrowserOrigin


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    display_name: str
    email: str | None


class OidcClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def authorization_url(
        self,
        *,
        browser: BrowserOrigin,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        metadata = await self._metadata(browser)
        parameters = {
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": browser.callback_url,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{metadata['authorization_endpoint']}?{urlencode(parameters)}"

    async def end_session_url(self, browser: BrowserOrigin) -> str | None:
        """Return the provider's browser logout URL when discovery exposes one."""
        metadata = await self._metadata(browser)
        endpoint = str(metadata.get("end_session_endpoint") or "").strip()
        if not endpoint:
            return None
        parameters = {
            "client_id": self.settings.oidc_client_id,
            "post_logout_redirect_uri": browser.app_origin,
        }
        return f"{endpoint}?{urlencode(parameters)}"

    async def exchange_and_verify(
        self,
        *,
        browser: BrowserOrigin,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> tuple[OidcIdentity, str | None]:
        metadata = await self._metadata(browser)
        token_endpoint = self._backend_url(str(metadata["token_endpoint"]))
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": browser.callback_url,
                    "client_id": self.settings.oidc_client_id,
                    "client_secret": self.settings.oidc_client_secret.get_secret_value(),
                    "code_verifier": code_verifier,
                },
            )
            response.raise_for_status()
            token = response.json()
            jwks_response = await client.get(self._backend_url(str(metadata["jwks_uri"])))
            jwks_response.raise_for_status()

        id_token = str(token.get("id_token") or "")
        if not id_token:
            raise ValueError("OIDC provider returned no ID token")
        key_set = KeySet.import_key_set(jwks_response.json())
        token_value = jwt.decode(
            id_token,
            key_set,
            algorithms=self._allowed_algorithms(),
        )
        claims = dict(token_value.claims)
        registry = jwt.JWTClaimsRegistry(
            leeway=30,
            iss={"essential": True, "value": browser.issuer},
            aud={"essential": True, "value": self.settings.oidc_client_id},
            exp={"essential": True},
            nonce={"essential": True, "value": expected_nonce},
        )
        registry.validate(claims)
        subject = str(claims.get("sub") or "")
        if not subject:
            raise ValueError("OIDC ID token has no subject")
        display_name = str(
            claims.get("name") or claims.get("preferred_username") or claims.get("email") or subject
        )
        email_value = str(claims.get("email") or "").strip() or None
        identity = OidcIdentity(
            issuer=self.settings.oidc_issuer,
            subject=subject,
            display_name=display_name,
            email=email_value,
        )
        refresh_token = str(token.get("refresh_token") or "").strip() or None
        return identity, refresh_token

    async def _metadata(self, browser: BrowserOrigin) -> dict[str, Any]:
        public = urlsplit(browser.keycloak_origin)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                self.settings.oidc_discovery_url,
                headers={
                    "Host": public.netloc,
                    "X-Forwarded-Host": public.netloc,
                    "X-Forwarded-Proto": public.scheme,
                },
            )
            response.raise_for_status()
        value = response.json()
        if value.get("issuer") != browser.issuer:
            raise ValueError("OIDC discovery issuer does not match configured issuer")
        return dict(value)

    def _backend_url(self, public_url: str) -> str:
        public = urlsplit(public_url)
        internal = urlsplit(self.settings.oidc_backend_base_url)
        return urlunsplit(
            (internal.scheme, internal.netloc, public.path, public.query, public.fragment)
        )

    def _allowed_algorithms(self) -> list[str]:
        values = [
            value.strip()
            for value in self.settings.oidc_signing_algorithms.split(",")
            if value.strip()
        ]
        if not values:
            raise ValueError("no OIDC signing algorithms are configured")
        return values
