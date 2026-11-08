"""Sign-in with Weave: `/login`, `/auth/callback`, `/logout`, and (stub mode only) `/auth/stub`.

See `docs/weave-integration.md` for the OIDC contract this implements against.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.services.users import sign_in
from krater.weave import WeaveAuthError, WeaveClient, WeaveUnavailableError, get_weave_client
from krater.weave.stub import StubWeaveClient
from krater.web.csrf import verify_csrf_token
from krater.web.deps import SESSION_USER_ID_KEY
from krater.web.templates import templates

router = APIRouter()
logger = logging.getLogger(__name__)

# Session keys used only for the duration of a single OIDC round trip; cleared (along with everything
# else) once sign-in succeeds, to guard against session fixation.
SESSION_STATE_KEY = "oauth_state"
SESSION_NONCE_KEY = "oauth_nonce"
SESSION_VERIFIER_KEY = "oauth_code_verifier"
SESSION_NEXT_KEY = "oauth_next"


def _redirect_uri() -> str:
    return f"{get_settings().base_url}/auth/callback"


def _safe_next_path(path: str | None) -> str:
    """A local path to send the browser to after sign-in, or `/` if `path` isn't one.

    Rejects anything that isn't an absolute path on this host: no scheme, no netloc, no `//` (which
    browsers treat as protocol-relative) -- otherwise `next` would be an open redirect. Backslashes and control
    characters are rejected too: browsers normalize `/\\evil.example` to `//evil.example`, and strip tabs/newlines.
    """
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    if "\\" in path or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return "/"
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return "/"
    return path


@router.get("/login")
def login(
    request: Request,
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    next: str | None = None,
) -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)

    request.session[SESSION_STATE_KEY] = state
    request.session[SESSION_NONCE_KEY] = nonce
    request.session[SESSION_VERIFIER_KEY] = code_verifier
    request.session[SESSION_NEXT_KEY] = _safe_next_path(next)

    url = weave_client.authorization_url(
        state=state, nonce=nonce, code_verifier=code_verifier, redirect_uri=_redirect_uri()
    )
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


def _sign_in_failed(request: Request, next_path: str, *, reason: str) -> HTMLResponse:
    """A page with a "Sign in again" button, for a sign-in that can't finish (an old or reused link, a sign-in
    started in another tab, the member saying no at Weave, or Weave rejecting the code), instead of bare JSON."""
    logger.warning("sign-in did not finish: %s", reason)
    again = "/login" if next_path == "/" else f"/login?{urlencode({'next': next_path})}"
    return templates.TemplateResponse(
        request, "auth/sign_in_failed.html", {"sign_in_again": again}, status_code=status.HTTP_400_BAD_REQUEST
    )


@router.get("/auth/callback")
def auth_callback(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    next_path = _safe_next_path(request.session.get(SESSION_NEXT_KEY))
    expected_state = request.session.get(SESSION_STATE_KEY)
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return _sign_in_failed(request, next_path, reason="invalid or expired state")
    if error or not code:
        # OAuth's error redirect (e.g. `access_denied`); `error` is Weave's short code, never a secret.
        return _sign_in_failed(request, next_path, reason=f"Weave sent no code (error={(error or 'none')[:64]!r})")

    nonce = request.session.get(SESSION_NONCE_KEY, "")
    code_verifier = request.session.get(SESSION_VERIFIER_KEY, "")

    try:
        identity = weave_client.exchange_code(
            code=code, code_verifier=code_verifier, redirect_uri=_redirect_uri(), nonce=nonce
        )
    except WeaveAuthError as exc:
        return _sign_in_failed(request, next_path, reason=f"Weave rejected the code: {str(exc)[:200]}")
    except WeaveUnavailableError as exc:
        logger.warning("sign-in could not reach Weave: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Weave is unavailable") from exc

    user = sign_in(db_session, identity)
    db_session.commit()

    # Session fixation: throw away everything the pre-login session held (state/nonce/verifier included)
    # before writing an authenticated identity into it.
    request.session.clear()
    request.session[SESSION_USER_ID_KEY] = str(user.id)
    request.session["weave_sub"] = user.weave_sub
    # The name rides in the signed session cookie only; Krater stores no member names (docs/weave-integration.md).
    request.session["display_name"] = identity.name or user.weave_sub

    return RedirectResponse(next_path, status_code=status.HTTP_302_FOUND)


@router.post("/logout", dependencies=[Depends(verify_csrf_token)])
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/auth/stub")
def stub_picker(request: Request, weave_client: Annotated[WeaveClient, Depends(get_weave_client)]):
    if get_settings().weave_mode == "live" or not isinstance(weave_client, StubWeaveClient):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    state = request.query_params.get("state", "")
    return templates.TemplateResponse(
        request,
        "auth/stub_picker.html",
        {"users": weave_client.list_all_users(), "state": state},
    )


__all__ = ["router"]
