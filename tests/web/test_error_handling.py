"""Error pages: the generic-exception handler (a plain page with no traceback in production, propagates
otherwise), and HTML 403/404 pages for browsers while API callers keep JSON."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.web.routers import pages as pages_router


def _break_home_page(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("boom: something went wrong deep in a service")

    monkeypatch.setattr(pages_router.project_service, "list_projects_for_user", _boom)


def test_unexpected_error_shows_a_generic_page_in_production(client: TestClient, login_as, monkeypatch) -> None:
    from tests.conftest import MEMBER_SUB

    login_as(MEMBER_SUB)
    _break_home_page(monkeypatch)
    monkeypatch.setattr(get_settings(), "env", "production")

    # `ServerErrorMiddleware` (which our `Exception` handler ends up registered on) sends the response
    # and then *always* re-raises the original exception too, for a real server's own logging -- so a
    # default `TestClient` (raise_server_exceptions=True) would turn that back into a raised error here
    # instead of handing back the response it already sent. `raise_server_exceptions=False` is what lets
    # us actually inspect that response.
    no_raise_client = TestClient(client.app, raise_server_exceptions=False)
    no_raise_client.cookies = client.cookies

    response = no_raise_client.get("/")

    assert response.status_code == 500
    assert "boom" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
    assert "Something went wrong" in response.text
    # Still gets the same security headers as everything else.
    assert "Content-Security-Policy" in response.headers


def test_unexpected_error_propagates_outside_production(client: TestClient, login_as, monkeypatch) -> None:
    from tests.conftest import MEMBER_SUB

    login_as(MEMBER_SUB)
    _break_home_page(monkeypatch)
    # `env` stays "test" here -- the default the whole suite runs under.

    with pytest.raises(RuntimeError, match="boom"):
        client.get("/")


def test_unknown_url_shows_the_not_found_page_to_browsers(client: TestClient) -> None:
    response = client.get("/no-such-page", headers={"Accept": "text/html,application/xhtml+xml"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Not found" in response.text


def test_unknown_url_stays_json_for_api_callers(client: TestClient) -> None:
    response = client.get("/no-such-page", headers={"Accept": "application/json"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_missing_role_shows_the_not_allowed_page_to_browsers(client: TestClient, login_as) -> None:
    from tests.conftest import MEMBER_SUB

    login_as(MEMBER_SUB)
    response = client.get("/reviews", headers={"Accept": "text/html"})

    assert response.status_code == 403
    assert "Not allowed" in response.text


def test_signed_out_redirect_is_not_turned_into_an_error_page(client: TestClient) -> None:
    response = client.get("/reviews", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_production_500_carries_its_request_id_into_the_log_and_the_page(
    client: TestClient, login_as, monkeypatch, caplog
) -> None:
    from tests.conftest import MEMBER_SUB

    login_as(MEMBER_SUB)
    _break_home_page(monkeypatch)
    monkeypatch.setattr(get_settings(), "env", "production")
    # Alembic's `fileConfig` (run when the test database is migrated) disables loggers that already exist.
    monkeypatch.setattr(logging.getLogger("krater.web.app"), "disabled", False)
    no_raise_client = TestClient(client.app, raise_server_exceptions=False)
    no_raise_client.cookies = client.cookies

    response = no_raise_client.get("/")

    request_id = response.headers["X-Request-ID"]
    assert request_id in response.text
    (record,) = [r for r in caplog.records if r.getMessage().startswith("unhandled exception")]
    assert record.request_id == request_id


def test_weave_being_down_shows_a_page_to_browsers(client: TestClient, login_as, weave_stub, monkeypatch) -> None:
    from krater.weave import WeaveUnavailableError
    from tests.conftest import MEMBER_SUB

    login_as(MEMBER_SUB)

    def _down(sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")

    monkeypatch.setattr(weave_stub, "get_user", _down)

    page = client.get("/projects/new", headers={"Accept": "text/html"})
    api = client.get("/projects/new", headers={"Accept": "application/json"})

    assert page.status_code == 503
    assert "Weave isn't answering" in page.text
    assert api.status_code == 503
    assert api.json() == {"detail": "Weave is unavailable"}


def test_the_api_docs_are_off_in_production(monkeypatch) -> None:
    from krater.web.app import create_app

    monkeypatch.setattr(get_settings(), "env", "production")
    app = create_app()

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
