"""Auth dependencies for HTML routes: who's signed in, and what Weave currently says they may do.

- `current_user`: the signed-in `User`, or `None`. For pages that render differently either way (e.g. the
  header's "sign in" link vs. the user's name) without requiring sign-in.
- `require_user`: the signed-in `User`, or a redirect to `/login?next=<this page>`. For any page that
  needs *a* signed-in user but doesn't itself gate on roles.
- `session_actor`: an `Actor` built from the user's `roles_cached` (what Weave said last). **Display and
  navigation only** -- e.g. deciding whether to show a "Review" link. Never use it to authorize an
  action: it can be stale.
- `fresh_actor`: an `Actor` built from a live Weave directory lookup (`krater.services.users.authorize`).
  Refuses (403) if Weave no longer lists the user as an active Ganymede member, and fails closed (503)
  if Weave can't be reached. **Every state-changing action must use this, or one of the two below,
  instead of `session_actor`.**
- `require_reviewer` / `require_admin`: `fresh_actor`, plus a 403 unless the actor is a reviewer/admin.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import User
from krater.services import users as user_service
from krater.services.actor import Actor
from krater.services.errors import NotAMember
from krater.weave import WeaveClient, WeaveUnavailableError, get_weave_client

SESSION_USER_ID_KEY = "user_id"


def current_user(request: Request, db_session: Annotated[Session, Depends(get_session)]) -> User | None:
    """The signed-in `User`, or `None` if there isn't one (no session, or it points at a deleted user)."""
    raw_user_id = request.session.get(SESSION_USER_ID_KEY)
    if not raw_user_id:
        return None
    try:
        user_id = uuid.UUID(raw_user_id)
    except ValueError:
        return None
    return db_session.get(User, user_id)


def require_user(request: Request, user: Annotated[User | None, Depends(current_user)]) -> User:
    """The signed-in `User`. Redirects (303) to `/login?next=<this request's path>` if signed out."""
    if user is not None:
        return user

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={quote(next_path, safe='')}"},
    )


def session_actor(user: Annotated[User, Depends(require_user)]) -> Actor:
    """An `Actor` from the roles Weave last reported. Display/navigation only -- see the module docstring."""
    return user_service.cached_actor(user)


def fresh_actor(
    user: Annotated[User, Depends(require_user)],
    db_session: Annotated[Session, Depends(get_session)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
) -> Actor:
    """An `Actor` built from a live Weave lookup. Refuses (403) if Weave no longer lists the user as an
    active Ganymede member, and answers 503 if Weave can't be reached. Use this (or
    `require_reviewer`/`require_admin`) for state-changing actions."""
    try:
        return user_service.authorize(db_session, weave_client, user)
    except NotAMember as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except WeaveUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Weave is unavailable") from exc


def require_reviewer(actor: Annotated[Actor, Depends(fresh_actor)]) -> Actor:
    """`fresh_actor`, plus a 403 unless the actor is a Ganymede reviewer."""
    if not actor.is_reviewer:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="reviewer access required")
    return actor


def require_admin(actor: Annotated[Actor, Depends(fresh_actor)]) -> Actor:
    """`fresh_actor`, plus a 403 unless the actor is a Ganymede admin."""
    if not actor.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin access required")
    return actor


__all__ = [
    "current_user",
    "fresh_actor",
    "require_admin",
    "require_reviewer",
    "require_user",
    "session_actor",
]
