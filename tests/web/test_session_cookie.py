"""Session cookie attributes: name, `SameSite`, `Secure` only in production, and a bounded lifetime."""

from __future__ import annotations

from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.web.app import SESSION_COOKIE_NAME


def _set_cookie_header(response) -> str:
    # httpx/TestClient exposes multiple Set-Cookie headers via .headers.get_list in recent versions;
    # for our purposes (asserting on attributes of *the* session cookie) grabbing the raw header text
    # for the cookie we care about is enough, and works across versions.
    for value in response.headers.get_list("set-cookie") if hasattr(response.headers, "get_list") else []:
        if value.startswith(f"{SESSION_COOKIE_NAME}="):
            return value
    # Fallback: a single Set-Cookie header.
    return response.headers.get("set-cookie", "")


def test_session_cookie_uses_the_configured_name(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)

    cookie_header = _set_cookie_header(response)
    assert f"{SESSION_COOKIE_NAME}=" in cookie_header


def test_session_cookie_is_samesite_lax(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)

    cookie_header = _set_cookie_header(response)
    assert "samesite=lax" in cookie_header.lower()


def test_session_cookie_not_secure_outside_production(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)

    cookie_header = _set_cookie_header(response)
    assert "secure" not in cookie_header.lower()


def test_session_cookie_is_secure_in_production(client: TestClient, monkeypatch) -> None:
    # The cookie's flags are baked in when the middleware is constructed (at `create_app()` time), so
    # this needs a fresh app built with `env=production` already set -- flipping the setting after the
    # fact (as most of this suite's `monkeypatch.setattr(get_settings(), ...)` calls do) wouldn't reach
    # `SessionMiddleware`'s already-constructed `https_only`.
    from krater.db import get_session
    from krater.web.app import create_app

    monkeypatch.setattr(get_settings(), "env", "production")
    app = create_app()
    app.dependency_overrides[get_session] = client.app.dependency_overrides[get_session]
    fresh_client = TestClient(app)

    response = fresh_client.get("/login", follow_redirects=False)

    cookie_header = _set_cookie_header(response)
    assert "secure" in cookie_header.lower()


def test_session_cookie_has_a_bounded_max_age(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)

    cookie_header = _set_cookie_header(response).lower()
    assert "max-age=" in cookie_header


def test_oidc_callback_round_trip_survives_samesite_lax(client: TestClient) -> None:
    """The state/nonce/verifier `/login` stashes in the session must still be there when the browser
    lands back on `/auth/callback` via a top-level GET redirect from Weave -- exactly the navigation
    `SameSite=Lax` is designed to still send cookies on."""
    from urllib.parse import parse_qs, urlparse

    from tests.conftest import MEMBER_SUB

    login_response = client.get("/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    callback_response = client.get(
        "/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False
    )

    assert callback_response.status_code == 302
