from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from ..audit import record_audit_event
from ..authorization.dependencies import CsrfProtected, CurrentActor, Database
from ..config import get_settings
from ..identity.origins import browser_origin
from ..identity.service import IdentityService

router = APIRouter(prefix="/auth", tags=["authentication"])


def identity_service() -> IdentityService:
    return IdentityService(get_settings())


Identity = Annotated[IdentityService, Depends(identity_service)]


@router.get("/login", response_class=RedirectResponse)
async def login(
    request: Request,
    session: Database,
    identity: Identity,
    return_path: Annotated[str, Query(max_length=500)] = "/",
) -> RedirectResponse:
    browser = browser_origin(request, get_settings())
    authorization_url = await identity.begin_login(
        session, browser=browser, return_path=return_path
    )
    return RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback", response_class=RedirectResponse)
async def callback(
    request: Request,
    session: Database,
    identity: Identity,
    state: Annotated[str, Query(min_length=16, max_length=500)],
    code: Annotated[str, Query(min_length=1, max_length=4000)],
) -> RedirectResponse:
    settings = get_settings()
    browser = browser_origin(request, settings)
    try:
        browser_session, return_path = await identity.complete_login(
            session, browser=browser, state=state, code=code
        )
    except httpx.HTTPError as error:
        record_audit_event(
            session,
            "auth.login_failed",
            details={"reason": "identity_provider_error"},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="identity provider login failed",
        ) from error
    except (ValueError, PermissionError) as error:
        record_audit_event(
            session,
            "auth.login_failed",
            details={"reason": str(error)[:500]},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    response = RedirectResponse(
        f"{browser.app_origin}{return_path}",
        status_code=status.HTTP_302_FOUND,
    )
    secure = settings.env == "production"
    max_age = max(0, int((browser_session.expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        settings.session_cookie_name,
        browser_session.token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        browser_session.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/session")
async def session_info(actor: CurrentActor) -> dict[str, object]:
    return {
        "authenticated": True,
        "principal": {
            "principal_id": str(actor.principal_id),
            "display_name": actor.display_name,
            "system_role": actor.system_role,
        },
    }


@router.post("/logout", response_class=JSONResponse)
async def logout(
    request: Request,
    session: Database,
    identity: Identity,
    _: CurrentActor,
    __: CsrfProtected,
) -> JSONResponse:
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name, "")
    if raw_token:
        await identity.revoke(session, raw_token=raw_token)
    provider_logout_url: str | None = None
    try:
        provider_logout_url = await identity.oidc.end_session_url(
            browser_origin(request, settings)
        )
    except (httpx.HTTPError, ValueError, KeyError, HTTPException):
        # Provider availability must not prevent local logout.
        provider_logout_url = None
    response = JSONResponse({"status": "logged_out", "provider_logout_url": provider_logout_url})
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return response
