"""The Help pages (`/help`): the guides in docs/guides, rendered, public, and with every link inside them working."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from krater.web import help_docs
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB

_HREF = re.compile(r'href="([^"]+)"')
_ID = re.compile(r'id="([^"]+)"')


def _guide_html(client: TestClient, slug: str) -> str:
    response = client.get(f"/help/{slug}")
    assert response.status_code == 200, slug
    return response.text


_PUBLIC = [guide for guide in help_docs.GUIDES if not guide.admins_only]
_ADMINS_ONLY = [guide for guide in help_docs.GUIDES if guide.admins_only]


def test_the_index_lists_every_public_guide_without_signing_in(client: TestClient):
    response = client.get("/help")

    assert response.status_code == 200
    for guide in _PUBLIC:
        assert f'href="/help/{guide.slug}"' in response.text
    assert "For reviewers and admins" in response.text


def test_the_ospool_guides_are_for_admins_only(client: TestClient, login_as):
    assert {guide.slug for guide in _ADMINS_ONLY} == {"ospool-grants", "share-with-ospool"}
    for who in (None, MEMBER_SUB, REVIEWER_SUB):
        if who is not None:
            login_as(who)
        index = client.get("/help").text
        assert "OSPool" not in index, who
        for guide in _ADMINS_ONLY:
            assert f'href="/help/{guide.slug}"' not in index, who
            assert client.get(f"/help/{guide.slug}").status_code == 404, who

    login_as(ADMIN_SUB)
    index = client.get("/help").text
    for guide in _ADMINS_ONLY:
        assert f'href="/help/{guide.slug}"' in index
        assert client.get(f"/help/{guide.slug}").status_code == 200


def test_no_public_guide_names_ospool(client: TestClient):
    for guide in _PUBLIC:
        page = _guide_html(client, guide.slug)
        assert "OSPool" not in page and "OSG" not in page, guide.slug
        for hidden in _ADMINS_ONLY:  # a link everyone else would follow to a 404
            assert f'href="/help/{hidden.slug}' not in page, (guide.slug, hidden.slug)


def test_every_guide_renders_with_its_own_title(client: TestClient, login_as):
    login_as(ADMIN_SUB)  # sees the admins-only guides too
    for guide in help_docs.GUIDES:
        page = _guide_html(client, guide.slug)
        rendered = help_docs.render(guide.slug)
        assert rendered is not None and rendered.title
        assert "<h1>" in page and rendered.title.split()[0] in page
        assert "```" not in page  # every fence closed and rendered


def test_an_unknown_guide_is_404(client: TestClient):
    assert client.get("/help/nope").status_code == 404
    assert client.get("/help/..%2FSPEC").status_code == 404


def test_every_link_in_the_guides_goes_somewhere_real(client: TestClient, login_as):
    login_as(ADMIN_SUB)
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


def test_a_link_to_another_doc_goes_to_github(client: TestClient, login_as):
    login_as(ADMIN_SUB)
    page = _guide_html(client, "ospool-grants")

    assert 'href="https://github.com/patchworklabsorg/krater/blob/main/docs/SPEC.md' in page


def test_the_top_bar_links_to_help(client: TestClient, login_as):
    assert 'href="/help"' in client.get("/gallery").text
    login_as(MEMBER_SUB)
    assert 'href="/help"' in client.get("/").text


def test_the_project_page_says_what_to_do_next(
    client: TestClient, login_as, create_project, submitted_project, approved_project
):
    member = login_as(MEMBER_SUB)
    draft = create_project(member)
    page = client.get(f"/projects/{draft.id}").text
    assert "<strong>Next:</strong>" in page and "Submit for review</strong>" in page

    pending = submitted_project(member)
    assert "reviewers are deciding" in client.get(f"/projects/{pending.id}").text

    reviewer = login_as(REVIEWER_SUB)
    approved = approved_project(member, reviewer)
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{approved.id}").text
    assert "Need more budget?" in page and "Start completion</strong>" in page
