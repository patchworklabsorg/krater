"""`SecurityHeadersMiddleware` and the no-inline-script rule it depends on."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.web.templates import TEMPLATES_DIR


def test_headers_present_on_a_plain_page(client: TestClient) -> None:
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert response.headers["Content-Security-Policy"]


def test_csp_has_no_inline_script_allowance(client: TestClient) -> None:
    csp = client.get("/").headers["Content-Security-Policy"]

    assert "'unsafe-inline'" not in csp
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp


def test_csp_includes_the_s3_public_origin(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "s3_public_endpoint_url", "http://storage.example.com:8333")

    csp = client.get("/").headers["Content-Security-Policy"]

    assert "img-src 'self' http://storage.example.com:8333" in csp
    assert "connect-src 'self' http://storage.example.com:8333" in csp
    assert "form-action 'self' http://storage.example.com:8333" in csp


def test_csp_falls_back_to_self_only_without_a_configured_s3_origin(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "s3_public_endpoint_url", "")

    csp = client.get("/").headers["Content-Security-Policy"]

    assert "img-src 'self'" in csp
    assert "img-src 'self' http" not in csp


def test_hsts_only_set_in_production(client: TestClient, monkeypatch) -> None:
    assert "Strict-Transport-Security" not in client.get("/").headers

    monkeypatch.setattr(get_settings(), "env", "production")
    assert "Strict-Transport-Security" in client.get("/").headers


def test_error_pages_also_carry_the_headers(client: TestClient) -> None:
    # Anonymous, so this actually 303s to /login first -- either way, headers stick.
    response = client.get("/projects/00000000-0000-0000-0000-000000000000", follow_redirects=False)

    assert response.status_code == 303
    assert "Content-Security-Policy" in response.headers


# --------------------------------------------------------------------------------------------------
# No inline scripts / event-handler attributes anywhere in the templates that actually render.
# --------------------------------------------------------------------------------------------------

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
_INLINE_EVENT_HANDLER = re.compile(r"""\son\w+\s*=\s*["']""", re.IGNORECASE)


def _assert_no_inline_scripting(html: str, where: str) -> None:
    assert not _INLINE_SCRIPT.search(html), f"inline <script> found in {where}"
    assert not _INLINE_EVENT_HANDLER.search(html), f"inline on*= handler found in {where}"


def test_templates_have_no_inline_scripts_or_handlers() -> None:
    """Static check over every template source: catches an inline `<script>`/`on*=` even in a template
    branch a single request wouldn't render (an error state, a different role's view, ...)."""
    for path in Path(TEMPLATES_DIR).rglob("*.html"):
        _assert_no_inline_scripting(path.read_text(), str(path))


def test_rendered_home_page_has_no_inline_scripting(client: TestClient) -> None:
    _assert_no_inline_scripting(client.get("/").text, "/")


def test_rendered_stub_picker_has_no_inline_scripting(client: TestClient) -> None:
    _assert_no_inline_scripting(client.get("/auth/stub").text, "/auth/stub")


def test_rendered_edit_page_has_no_inline_scripting(client: TestClient, login_as, create_project) -> None:
    from tests.conftest import MEMBER_SUB

    user = login_as(MEMBER_SUB)
    project = create_project(user)

    _assert_no_inline_scripting(client.get(f"/projects/{project.id}/edit").text, "/projects/{id}/edit")
