"""Flash messages: a one-time message stashed in the session and shown on the next page render.

Used for `InvalidState` ("that doesn't make sense right now") and for confirming an action succeeded,
since state-changing routes redirect (POST/redirect/GET) rather than rendering directly.
"""

from __future__ import annotations

from typing import Literal

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates

_SESSION_KEY = "_flashes"

Category = Literal["success", "error", "info"]


def flash(request: Request, message: str, category: Category = "info") -> None:
    """Queue `message` to be shown once, on the next page this session renders."""
    messages = request.session.get(_SESSION_KEY, [])
    messages.append({"message": message, "category": category})
    request.session[_SESSION_KEY] = messages


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Return and clear every queued flash message for this session."""
    return request.session.pop(_SESSION_KEY, [])


@jinja2.pass_context
def _get_flashes_template_global(context: dict) -> list[dict[str, str]]:
    request: Request = context["request"]
    return pop_flashes(request)


def register_flash_template_global(templates: Jinja2Templates) -> None:
    """Wire `{{ get_flashes() }}` into `templates`, bound to the current request."""
    templates.env.globals["get_flashes"] = _get_flashes_template_global


__all__ = ["flash", "pop_flashes", "register_flash_template_global"]
