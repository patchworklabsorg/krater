"""CSRF protection for session-authenticated form posts.

Every state-changing form includes a hidden `csrf_token` field, filled in via the `csrf_token()` Jinja
global (registered onto `templates` by `register_csrf_template_global`). `verify_csrf_token` is a FastAPI
dependency that checks the posted `csrf_token` form field against the one stored in the session; add it
to any POST route with `dependencies=[Depends(verify_csrf_token)]`.
"""

from __future__ import annotations

import secrets
from typing import Annotated

import jinja2
from fastapi import Form, HTTPException, Request, status
from fastapi.templating import Jinja2Templates

#: The session key the token is stored under.
CSRF_SESSION_KEY = "csrf_token"

#: The form field name every CSRF-protected form must post the token under.
CSRF_FORM_FIELD = "csrf_token"


def get_or_create_csrf_token(request: Request) -> str:
    """This session's CSRF token, generating and storing one on first use."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def verify_csrf_token(request: Request, csrf_token: Annotated[str | None, Form()] = None) -> None:
    """Raise 403 unless the posted `csrf_token` form field matches this session's token.

    `csrf_token` is optional at the FastAPI level (rather than required) so that a request missing it
    entirely fails with a clear 403, the same as a mismatched one, instead of a generic 422.
    """
    expected = request.session.get(CSRF_SESSION_KEY)
    if not expected or not csrf_token or not secrets.compare_digest(csrf_token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid or missing CSRF token")


@jinja2.pass_context
def _csrf_token_template_global(context: dict) -> str:
    request: Request = context["request"]
    return get_or_create_csrf_token(request)


def register_csrf_template_global(templates: Jinja2Templates) -> None:
    """Wire `{{ csrf_token() }}` into `templates`, bound to the current request via the Jinja context."""
    templates.env.globals["csrf_token"] = _csrf_token_template_global


__all__ = [
    "CSRF_FORM_FIELD",
    "CSRF_SESSION_KEY",
    "get_or_create_csrf_token",
    "register_csrf_template_global",
    "verify_csrf_token",
]
