"""Browser tab titles: every page gives only its own name, and `base.html` adds " · Krater" once."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_SUB, MEMBER_SUB

TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
SUFFIX = " &middot; Krater"


def _title(html: str) -> str:
    match = TITLE_RE.search(html)
    assert match is not None, "page has no <title>"
    return match.group(1)


def test_home_title_is_the_app_name(client: TestClient) -> None:
    assert _title(client.get("/").text) == "Krater"


@pytest.mark.parametrize(
    ("path", "page_name"),
    [
        ("/gallery", "Gallery"),
        ("/pricing", "GPU pricing"),
        ("/does-not-exist", "Not found"),
    ],
)
def test_public_page_titles_end_with_the_app_name(client: TestClient, path: str, page_name: str) -> None:
    # Error pages are HTML only for a browser; other clients get JSON.
    response = client.get(path, headers={"accept": "text/html"})
    assert _title(response.text) == page_name + SUFFIX


def test_signed_in_page_titles_end_with_the_app_name(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Titled <Project>")

    assert _title(client.get("/projects/new").text) == "New proposal" + SUFFIX
    assert _title(client.get(f"/projects/{project.id}").text) == "Titled &lt;Project&gt;" + SUFFIX
    assert _title(client.get(f"/projects/{project.id}/edit").text) == "Edit draft" + SUFFIX

    login_as(ADMIN_SUB)
    assert _title(client.get("/reviews").text) == "Review queue" + SUFFIX
    assert _title(client.get("/admin").text) == "Admin" + SUFFIX


def test_admin_title_holds_no_html_and_the_quilt_section_is_in_the_body(client: TestClient, login_as) -> None:
    login_as(ADMIN_SUB)

    html = client.get("/admin").text
    title = _title(html)

    assert "<" not in title
    assert "Quilt" not in title
    body = html.split("<body>", 1)[1]
    assert 'id="quilt"' in body
    assert "Delivery to Quilt" in body
