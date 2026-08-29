from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record_audit_event
from ..config import Settings
from ..models import (
    ExternalIdentity,
    Library,
    LibraryMembership,
    OidcLoginAttempt,
    Principal,
    PrincipalSystemRole,
    WebSession,
)
from .oidc import OidcClient, OidcIdentity
from .origins import BrowserOrigin
from .security import SecretCipher, hash_token, pkce_challenge, random_token


@dataclass(frozen=True)
class BrowserSession:
    token: str
    csrf_token: str
    expires_at: datetime
    principal: Principal


class IdentityService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.oidc = OidcClient(settings)
        self.cipher = SecretCipher(settings.session_secret.get_secret_value())

    async def begin_login(
        self, session: AsyncSession, *, browser: BrowserOrigin, return_path: str
    ) -> str:
        state = random_token()
        nonce = random_token()
        verifier = random_token(64)
        attempt = OidcLoginAttempt(
            state_hash=hash_token(state),
            nonce=nonce,
            code_verifier_ciphertext=self.cipher.encrypt(verifier),
            return_path=self._safe_return_path(return_path),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self.settings.oidc_attempt_ttl_seconds),
        )
        session.add(attempt)
        await session.commit()
        return await self.oidc.authorization_url(
            browser=browser,
            state=state,
            nonce=nonce,
            code_challenge=pkce_challenge(verifier),
        )

    async def complete_login(
        self,
        session: AsyncSession,
        *,
        browser: BrowserOrigin,
        state: str,
        code: str,
    ) -> tuple[BrowserSession, str]:
        attempt = await self._consume_attempt(session, state)
        identity, refresh_token = await self.oidc.exchange_and_verify(
            browser=browser,
            code=code,
            code_verifier=self.cipher.decrypt(attempt.code_verifier_ciphertext),
            expected_nonce=attempt.nonce,
        )
        principal = await self._provision_principal(session, identity)
        browser_session = self._new_browser_session(principal, refresh_token)
        session.add(browser_session[0])
        await session.flush()
        record_audit_event(
            session,
            "auth.login_succeeded",
            actor_principal_id=principal.principal_id,
            subject_principal_id=principal.principal_id,
            session_id=browser_session[0].session_id,
            details={"issuer": identity.issuer},
        )
        await session.commit()
        return browser_session[1], attempt.return_path

    async def revoke(self, session: AsyncSession, *, raw_token: str) -> None:
        record = await session.scalar(
            select(WebSession).where(WebSession.token_hash == hash_token(raw_token))
        )
        if record is not None and record.revoked_at is None:
            record.revoked_at = datetime.now(UTC)
            record_audit_event(
                session,
                "auth.session_revoked",
                actor_principal_id=record.principal_id,
                subject_principal_id=record.principal_id,
                session_id=record.session_id,
                details={"reason": "user_logout"},
            )
            await session.commit()

    async def _consume_attempt(self, session: AsyncSession, state: str) -> OidcLoginAttempt:
        attempt = await session.scalar(
            select(OidcLoginAttempt)
            .where(OidcLoginAttempt.state_hash == hash_token(state))
            .with_for_update()
        )
        now = datetime.now(UTC)
        if attempt is None or attempt.consumed_at is not None or attempt.expires_at <= now:
            raise ValueError("OIDC login attempt is invalid or expired")
        attempt.consumed_at = now
        await session.commit()
        return attempt

    async def _provision_principal(
        self, session: AsyncSession, identity: OidcIdentity
    ) -> Principal:
        identity_lock = f"{identity.issuer}\x1f{identity.subject}"
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": identity_lock},
        )
        external = await session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.issuer == identity.issuer,
                ExternalIdentity.subject == identity.subject,
            )
        )
        if external is not None:
            principal = await session.get(Principal, external.principal_id)
            if principal is None or principal.status != "ACTIVE":
                raise PermissionError("principal is not active")
            await session.execute(
                text("SELECT set_config('app.principal_id', :principal_id, true)"),
                {"principal_id": str(principal.principal_id)},
            )
            external.email = identity.email
            principal.display_name = identity.display_name
            await self._ensure_bootstrap_role(session, principal, identity.email)
            return principal

        principal = Principal(display_name=identity.display_name, status="ACTIVE")
        session.add(principal)
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.principal_id', :principal_id, true)"),
            {"principal_id": str(principal.principal_id)},
        )
        session.add(
            ExternalIdentity(
                principal_id=principal.principal_id,
                issuer=identity.issuer,
                subject=identity.subject,
                email=identity.email,
            )
        )
        session.add(
            PrincipalSystemRole(
                principal_id=principal.principal_id,
                role="ADMIN" if self._is_bootstrap_admin(identity.email) else "USER",
            )
        )
        personal_library = Library(
            library_type="PERSONAL",
            name=f"{identity.display_name}'s Library",
            owner_principal_id=principal.principal_id,
            status="ACTIVE",
            revision=1,
        )
        session.add(personal_library)
        await session.flush()
        session.add(
            LibraryMembership(
                library_id=personal_library.library_id,
                principal_id=principal.principal_id,
                role="OWNER",
                status="ACTIVE",
            )
        )
        return principal

    async def _ensure_bootstrap_role(
        self,
        session: AsyncSession,
        principal: Principal,
        email: str | None,
    ) -> None:
        if not self._is_bootstrap_admin(email):
            return
        assignment = await session.get(PrincipalSystemRole, principal.principal_id)
        if assignment is None:
            session.add(PrincipalSystemRole(principal_id=principal.principal_id, role="ADMIN"))
        else:
            assignment.role = "ADMIN"

    def _is_bootstrap_admin(self, email: str | None) -> bool:
        normalized = str(email or "").strip().lower()
        configured = {
            value.strip().lower()
            for value in self.settings.bootstrap_admin_emails.split(",")
            if value.strip()
        }
        return bool(normalized and normalized in configured)

    def _new_browser_session(
        self, principal: Principal, refresh_token: str | None
    ) -> tuple[WebSession, BrowserSession]:
        raw_token = random_token(48)
        csrf_token = random_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.web_session_ttl_seconds)
        record = WebSession(
            principal_id=principal.principal_id,
            token_hash=hash_token(raw_token),
            csrf_token_hash=hash_token(csrf_token),
            expires_at=expires_at,
            oidc_refresh_token_ciphertext=(
                self.cipher.encrypt(refresh_token) if refresh_token else None
            ),
        )
        value = BrowserSession(
            token=raw_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
            principal=principal,
        )
        return record, value

    @staticmethod
    def _safe_return_path(value: str) -> str:
        path = str(value or "/").strip()
        return path if path.startswith("/") and not path.startswith("//") else "/"
