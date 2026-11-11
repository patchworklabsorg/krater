"""The Help pages (`/help`): the guides in docs/guides, rendered, public, and with every link inside them working."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from krater.web import help_docs
from tests.conftest import MEMBER_SUB

_HREF = re.compile(r'href="([^"]+)"')
_ID = re.compile(r'id="([^"]+)"')


def _guide_html(client: TestClient, slug: str) -> str:
    response = client.get(f"/help/{slug}")
    assert response.status_code == 200, slug
    return response.text


def test_the_index_lists_every_guide_without_signing_in(client: TestClient):
    response = client.get("/help")

    assert response.status_code == 200
    for guide in help_docs.GUIDES:
        assert f'href="/help/{guide.slug}"' in response.text
    assert "For reviewers and admins" in response.text


def test_every_guide_renders_with_its_own_title(client: TestClient):
    for guide in help_docs.GUIDES:
        page = _guide_html(client, guide.slug)
        rendered = help_docs.render(guide.slug)
        assert rendered is not None and rendered.title
        assert "<h1>" in page and rendered.title.split()[0] in page
        assert "```" not in page  # every fence closed and rendered


def test_an_unknown_guide_is_404(client: TestClient):
    assert client.get("/help/nope").status_code == 404
    assert client.get("/help/..%2FSPEC").status_code == 404


def test_every_link_in_the_guides_goes_somewhere_real(client: TestClient):
    pages = {guide.slug: _guide_html(client, guide.slug) for guide in help_docs.GUIDES}
    ids = {slug: set(_ID.findall(page)) for slug, page in pages.items()}
    for slug, page in pages.items():
        body = page.split('class="card prose help-guide"', 1)[1]
        for href in _HREF.findall(body):
            if href.startswith("#"):
                assert href[1:] in ids[slug], (slug, href)
            elif href.startswith("/help/"):
                target, _, fragment = href.removeprefix("/help/").partition("#")
                assert target in pages, (slug, href)
                assert not fragment or fragment in ids[target], (slug, href)
            elif href.startswith("/"):
                assert client.get(href, follow_redirects=False).status_code in (200, 303), (slug, href)
            else:
                assert href.startswith(("https://", "http://", "mailto:")), (slug, href)  # never a bare .md


def test_a_link_to_another_doc_goes_to_github(client: TestClient):
    page = _guide_html(client, "ospool-grants")

    assert 'href="https://github.com/patchworklabsorg/krater/blob/main/docs/SPEC.md' in page


def test_the_top_bar_links_to_help(client: TestClient, login_as):
    assert 'href="/help"' in client.get("/gallery").text
    login_as(MEMBER_SUB)
    assert 'href="/help"' in client.get("/").text
