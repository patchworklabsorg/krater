"""The stub sign-in flow end to end, and everything that should reject it."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import User
from krater.weave import StubWeaveClient, WeaveUnavailableError

# Fixture subs from krater/weave/stub_users.json.
MEMBER_SUB = "PWLMEMBERONE"
NO_ROLE_SUB = "PWLNOROLE"
REVIEWER_SUB = "PWLREVIEWERONE"
FALLBACK_REVIEWER_SUB = "PWLREVIEWERTWO"  # no `roles` field: roles come from group slugs


def _start_login(client: TestClient, next: str | None = None) -> str:
    """GET /login, and return the `state` value it put in the stub-redirect URL."""
    params = {"next": next} if next else {}
    response = client.get("/login", params=params, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/auth/stub?")
    return parse_qs(urlparse(location).query)["state"][0]


def test_full_stub_sign_in_flow(client: TestClient, db_session: Session) -> None:
    # 1. /login redirects to the stub picker.
    state = _start_login(client)

    # 2. The stub picker lists fixture users to click.
    picker = client.get("/auth/stub", params={"state": state})
    assert picker.status_code == 200
    assert "Mia Member" in picker.text

    # 3. Picking a user posts/links to the callback with code=<sub>.
    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)
    assert callback.status_code == 302
    assert callback.headers["location"] == "/"

    user = db_session.execute(select(User).where(User.weave_sub == MEMBER_SUB)).scalar_one()
    assert user.roles_cached == ["ganymede:member"]
    assert user.slack_user_id == "U0001MEMBER"  # from Weave's `slack_id` claim
    # The name lives in the session cookie only, never in Krater's database.
    assert "Mia Member" in client.get("/").text
    assert user.last_login_at is not None

    # 4. The signed-in page shows the user's name and a sign-out control.
    home = client.get("/")
    assert home.status_code == 200
    assert "Mia Member" in home.text
    assert "Sign out" in home.text

    # 5. Logging out (with a valid CSRF token, picked up from the page) clears the session.
    csrf_token = home.text.split('name="csrf_token" value="')[1].split('"')[0]
    logout = client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert logout.status_code == 302

    after_logout = client.get("/")
    assert "Sign out" not in after_logout.text
    assert "Sign in" in after_logout.text


def test_login_next_round_trips_to_the_final_redirect(client: TestClient) -> None:
    state = _start_login(client, next="/somewhere/safe")

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    assert callback.headers["location"] == "/somewhere/safe"


def test_callback_rejects_a_missing_state(client: TestClient) -> None:
    _start_login(client)

    response = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": "not-the-real-state"})

    assert response.status_code == 400


def test_callback_rejects_when_no_login_was_started(client: TestClient) -> None:
    response = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": "anything"})

    assert response.status_code == 400


def test_a_failed_sign_in_shows_a_page_with_sign_in_again(client: TestClient) -> None:
    state = _start_login(client, next="/projects")

    # The stub rejects a code that isn't a fixture sub, the way Weave rejects an old or reused code.
    response = client.get("/auth/callback", params={"code": "NOT-A-STUB-USER", "state": state})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Sign in again" in response.text
    assert 'href="/login?next=%2Fprojects"' in response.text


def test_a_stale_state_shows_the_sign_in_again_page(client: TestClient) -> None:
    response = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": "anything"})

    assert response.status_code == 400
    assert 'href="/login"' in response.text


@pytest.mark.parametrize("params", [{"error": "access_denied"}, {}])
def test_a_callback_without_a_code_shows_the_sign_in_again_page(client: TestClient, params: dict) -> None:
    state = _start_login(client)

    response = client.get("/auth/callback", params={"state": state, **params})

    assert response.status_code == 400
    assert "Sign in again" in response.text


def test_weave_being_unavailable_at_the_callback_shows_try_again(client: TestClient, monkeypatch) -> None:
    def _unavailable(self, **kwargs):
        raise WeaveUnavailableError("Weave's token endpoint returned 429")

    monkeypatch.setattr(StubWeaveClient, "exchange_code", _unavailable)
    state = _start_login(client)

    response = client.get(
        "/auth/callback", params={"code": MEMBER_SUB, "state": state}, headers={"accept": "text/html"}
    )

    assert response.status_code == 503
    assert "Try again in a minute" in response.text


def test_anyone_weave_signs_in_is_a_member_with_no_role(client: TestClient, db_session: Session) -> None:
    state = _start_login(client)

    response = client.get("/auth/callback", params={"code": NO_ROLE_SUB, "state": state}, follow_redirects=False)

    assert response.status_code == 302
    user = db_session.execute(select(User).where(User.weave_sub == NO_ROLE_SUB)).scalar_one()
    assert user.roles_cached == ["ganymede:member"]
    assert user.last_login_at is not None
    assert "Sign out" in client.get("/").text


def test_callback_maps_group_slugs_when_weave_sends_no_roles(client: TestClient, db_session: Session) -> None:
    state = _start_login(client)

    response = client.get(
        "/auth/callback", params={"code": FALLBACK_REVIEWER_SUB, "state": state}, follow_redirects=False
    )

    assert response.status_code == 302
    user = db_session.execute(select(User).where(User.weave_sub == FALLBACK_REVIEWER_SUB)).scalar_one()
    assert user.roles_cached == ["ganymede:member", "ganymede:reviewer"]


def test_sign_in_drops_a_role_weave_removed(
    client: TestClient, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_roles(REVIEWER_SUB, [])
    state = _start_login(client)

    client.get("/auth/callback", params={"code": REVIEWER_SUB, "state": state}, follow_redirects=False)

    user = db_session.execute(select(User).where(User.weave_sub == REVIEWER_SUB)).scalar_one()
    assert user.roles_cached == ["ganymede:member"]


def test_admin_users_page_is_gone(client: TestClient, login_as) -> None:
    login_as("PWLADMINONE")

    assert client.get("/admin/users").status_code == 404


def test_logout_without_csrf_token_is_rejected(client: TestClient) -> None:
    state = _start_login(client)
    client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    response = client.post("/logout")

    assert response.status_code == 403
    # The session survives: still signed in.
    assert "Mia Member" in client.get("/").text


def test_login_rejects_an_open_redirect_in_next(client: TestClient) -> None:
    state = _start_login(client, next="https://evil.example/steal")

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    # Falls back to "/" instead of honoring the attacker-controlled absolute URL.
    assert callback.headers["location"] == "/"


def test_login_rejects_a_protocol_relative_next(client: TestClient) -> None:
    state = _start_login(client, next="//evil.example/steal")

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    assert callback.headers["location"] == "/"


@pytest.mark.parametrize("next_path", ["/\\evil.example/steal", "/\t/evil.example", "/%0a/ok\n//evil.example"])
def test_login_rejects_next_paths_browsers_turn_into_other_hosts(client: TestClient, next_path: str) -> None:
    state = _start_login(client, next=next_path)

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    assert callback.headers["location"] == "/"


def test_stub_routes_404_in_live_mode(client: TestClient, monkeypatch) -> None:
    from krater.config import get_settings

    monkeypatch.setattr(get_settings(), "weave_mode", "live")

    response = client.get("/auth/stub")

    assert response.status_code == 404
