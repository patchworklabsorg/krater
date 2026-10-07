"""Sign-in with Weave: `/login`, `/auth/callback`, `/logout`, and (stub mode only) `/auth/stub`.

See `docs/weave-integration.md` for the OIDC contract this implements against.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
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


@router.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str,
    state: str,
    db_session: Annotated[Session, Depends(get_session)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    expected_state = request.session.get(SESSION_STATE_KEY)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid or expired state")

    nonce = request.session.get(SESSION_NONCE_KEY, "")
    code_verifier = request.session.get(SESSION_VERIFIER_KEY, "")
    next_path = _safe_next_path(request.session.get(SESSION_NEXT_KEY))

    try:
        identity = weave_client.exchange_code(
            code=code, code_verifier=code_verifier, redirect_uri=_redirect_uri(), nonce=nonce
        )
    except WeaveAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sign-in with Weave failed") from exc
    except WeaveUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Weave is unavailable") from exc

    result = sign_in(db_session, identity)
    # Committed whatever the outcome, so the user's cached roles match what Weave just said.
    db_session.commit()
    if result.status == "not_a_member":
        # Also drops any earlier signed-in session in this browser: a refused sign-in leaves nobody signed in.
        request.session.clear()
        return templates.TemplateResponse(request, "auth/not_a_member.html", status_code=status.HTTP_403_FORBIDDEN)
    user = result.user

    # Session fixation: throw away everything the pre-login session held (state/nonce/verifier included)
    # before writing an authenticated identity into it.
    request.session.clear()
    request.session[SESSION_USER_ID_KEY] = str(user.id)
    request.session["weave_sub"] = user.weave_sub
    request.session["display_name"] = user.display_name

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
