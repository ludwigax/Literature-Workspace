"""Exercise the local browser OIDC flow without requiring a GUI browser."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8020")
    parser.add_argument("--username", default="alice")
    parser.add_argument("--password", default="alice-local")
    args = parser.parse_args()

    with httpx.Client(timeout=20) as client:
        start = client.get(f"{args.api}/api/v2/auth/login", follow_redirects=True)
        start.raise_for_status()

        form_parser = _LoginFormParser()
        form_parser.feed(start.text)
        if not form_parser.action:
            raise RuntimeError("Keycloak login form was not found")

        # Browsers treat loopback origins as trustworthy for Secure cookies.
        # httpx intentionally follows the stricter wire-level rule, so relax
        # only the local Keycloak cookies for this headless smoke client.
        if start.url.host in {"127.0.0.1", "localhost", "::1"}:
            for cookie in client.cookies.jar:
                cookie.secure = False

        submit = client.post(
            urljoin(str(start.url), form_parser.action),
            data={
                "username": args.username,
                "password": args.password,
                "credentialId": "",
            },
            follow_redirects=False,
        )
        if submit.status_code not in {302, 303} or not submit.headers.get("location"):
            raise RuntimeError(
                f"Keycloak login failed with HTTP {submit.status_code}; body={submit.text[:500]}"
            )

        callback = client.get(submit.headers["location"], follow_redirects=False)
        if callback.status_code not in {302, 303}:
            raise RuntimeError(
                f"application callback failed with HTTP {callback.status_code}: {callback.text}"
            )

        session = client.get(f"{args.api}/api/v2/auth/session")
        session.raise_for_status()
        libraries = client.get(f"{args.api}/api/v2/libraries")
        libraries.raise_for_status()

        payload = session.json()
        library_payload = libraries.json()
        csrf_token = client.cookies.get("litv2_csrf")
        logout = client.post(
            f"{args.api}/api/v2/auth/logout",
            headers={"X-CSRF-Token": csrf_token or ""},
        )
        logout.raise_for_status()
        after_logout = client.get(f"{args.api}/api/v2/auth/session")
        if after_logout.status_code != 401:
            raise RuntimeError(
                f"revoked application session remained active ({after_logout.status_code})"
            )
        provider_logout_url = logout.json().get("provider_logout_url")
        if not provider_logout_url:
            raise RuntimeError("OIDC provider exposed no end-session URL")
        provider_logout = client.get(provider_logout_url, follow_redirects=True)
        provider_logout.raise_for_status()

        login_again = client.get(f"{args.api}/api/v2/auth/login", follow_redirects=True)
        login_parser = _LoginFormParser()
        login_parser.feed(login_again.text)
        if not login_parser.action:
            raise RuntimeError("provider SSO session remained active after logout")
        print(
            "OIDC smoke passed:",
            payload["principal"]["display_name"],
            f"libraries={len(library_payload)}",
            "logout=provider-ended",
        )


if __name__ == "__main__":
    main()
