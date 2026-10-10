"""`fresh_actor` must ask Weave on every call (and fail closed when it can't), and `require_user` must
actually redirect signed-out requests to `/login`.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import User
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.weave import StubWeaveClient, WeaveUnavailableError
from krater.web.app import create_app
from krater.web.deps import (
    current_user,
    fresh_actor,
    require_admin,
    require_reviewer,
    require_user,
    session_actor,
)

SUB = "PWLDEPSTEST"


#: What `fresh_actor` reads off the request: only the method, which decides whether a cached directory answer will do.
_POST = Request({"type": "http", "method": "POST", "headers": []})


def _make_request(session_data: dict) -> object:
    class _FakeRequest:
        session = session_data

    return _FakeRequest()


def _make_user(db_session: Session, *, roles_cached: list[str] | None = None) -> User:
    user = User(
        weave_sub=SUB,
        display_name="Dep Test",
        email="dep@example.com",
        email_verified=True,
        roles_cached=roles_cached or [],
    )
    db_session.add(user)
    db_session.flush()
    return user


def _weave(roles: list[str] | None) -> StubWeaveClient:
    weave = StubWeaveClient()
    weave.put_user(SUB, name="Dep Test", email="dep@example.com", roles=roles)
    return weave


def test_current_user_is_none_without_a_session(db_session: Session) -> None:
    assert current_user(_make_request({}), db_session) is None


def test_current_user_looks_up_the_session_user_id(db_session: Session) -> None:
    user = _make_user(db_session)

    found = current_user(_make_request({"user_id": str(user.id)}), db_session)

    assert found is not None
    assert found.id == user.id


def test_current_user_ignores_a_corrupt_session_value(db_session: Session) -> None:
    assert current_user(_make_request({"user_id": "not-a-uuid"}), db_session) is None


def test_fresh_actor_sees_a_role_weave_removed_after_sign_in(db_session: Session) -> None:
    user = _make_user(db_session)
    weave = _weave(["member", "reviewer"])
    assert fresh_actor(_POST, user, db_session, weave).is_reviewer

    weave.set_roles(SUB, ["member"])
    actor = fresh_actor(_POST, user, db_session, weave)

    assert isinstance(actor, Actor)
    assert actor.is_member
    assert not actor.is_reviewer
    with pytest.raises(HTTPException) as exc_info:
        require_reviewer(actor)
    assert exc_info.value.status_code == 403


def test_fresh_actor_refuses_once_weave_revokes_the_member_role(db_session: Session) -> None:
    user = _make_user(db_session)
    weave = _weave(["member", "admin"])
    weave.set_roles(SUB, ["admin"])

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(_POST, user, db_session, weave)
    assert exc_info.value.status_code == 403


def test_fresh_actor_refuses_a_user_the_directory_answers_404_for(db_session: Session) -> None:
    user = _make_user(db_session)
    weave = _weave(["member"])
    weave.remove_user(SUB)

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(_POST, user, db_session, weave)
    assert exc_info.value.status_code == 403


def test_fresh_actor_refuses_a_user_weave_locked(db_session: Session) -> None:
    user = _make_user(db_session)
    weave = _weave(["member"])
    weave.set_active(SUB, False)

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(_POST, user, db_session, weave)
    assert exc_info.value.status_code == 403


def test_fresh_actor_fails_closed_with_503_when_weave_is_unreachable(db_session: Session) -> None:
    user = _make_user(db_session)

    class _DownWeave(StubWeaveClient):
        def get_user(self, sub: str, *, fresh: bool = False):
            raise WeaveUnavailableError("down")

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(_POST, user, db_session, _DownWeave())
    assert exc_info.value.status_code == 503


@pytest.mark.parametrize(
    ("method", "expect_fresh"), [("POST", True), ("DELETE", True), ("GET", False), ("HEAD", False)]
)
def test_fresh_actor_skips_the_directory_cache_for_anything_but_a_page_view(
    db_session: Session, method: str, expect_fresh: bool
) -> None:
    user = _make_user(db_session)
    seen: list[bool] = []

    class _RecordingWeave(StubWeaveClient):
        def get_user(self, sub: str, *, fresh: bool = False):
            seen.append(fresh)
            return super().get_user(sub, fresh=fresh)

    weave = _RecordingWeave()
    weave.put_user(SUB, name="Dep Test", email="dep@example.com", roles=["member"])

    fresh_actor(Request({"type": "http", "method": method, "headers": []}), user, db_session, weave)

    assert seen == [expect_fresh]


def test_session_actor_reports_cached_roles_without_asking_weave(db_session: Session) -> None:
    user = _make_user(db_session, roles_cached=[GROUP_REVIEWER])

    actor = session_actor(user)

    assert actor.groups == frozenset({GROUP_REVIEWER})


def test_require_admin_needs_the_admin_role(db_session: Session) -> None:
    user = _make_user(db_session)
    weave = _weave(["member", "reviewer"])

    with pytest.raises(HTTPException) as exc_info:
        require_admin(fresh_actor(_POST, user, db_session, weave))
    assert exc_info.value.status_code == 403

    weave.set_roles(SUB, ["member", "admin"])
    admin_actor = fresh_actor(_POST, user, db_session, weave)
    assert require_admin(admin_actor) is admin_actor
    assert user.roles_cached == ["ganymede:admin", GROUP_MEMBER]


def test_require_user_redirects_to_login_with_next_when_signed_out(db_session: Session) -> None:
    """An end-to-end check (through real FastAPI dependency resolution) that `require_user` actually
    produces the redirect, not just that the plain-Python-call behavior above is right."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session

    probe_router = APIRouter()

    @probe_router.get("/__test/require-user")
    def _probe(user: Annotated[User, Depends(require_user)]) -> dict:
        return {"id": str(user.id)}

    app.include_router(probe_router)

    with TestClient(app, follow_redirects=False) as test_client:
        response = test_client.get("/__test/require-user?foo=bar")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F__test%2Frequire-user%3Ffoo%3Dbar"
