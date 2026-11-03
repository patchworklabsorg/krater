"""HTTP-level tests for the public gallery: /gallery, /gallery/{id}. No auth required."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import GalleryMinorNames, ReviewDecision, ReviewSource
from krater.services import projects as project_service
from krater.services import screenshots as screenshot_service
from krater.services import site_settings
from krater.storage import get_object_store
from tests.conftest import MEMBER_SUB, REVIEWER_SUB
from tests.web.conftest import PNG_SIGNATURE, actor_for


def _completed_project(
    db_session: Session,
    approved_project,
    member,
    reviewer,
    weave_stub,
    *,
    with_screenshot: bool = False,
    write_up: str | None = None,
):
    project = approved_project(member, reviewer, title="Gallery Project")
    project_service.start_completion(db_session, actor_for(member), project=project)
    project_service.update_draft(
        db_session,
        actor_for(member),
        project=project,
        demo_url="https://example.com/demo",
        tags=["ganymede", "rover"],
        **({"write_up": write_up} if write_up is not None else {}),
    )
    if with_screenshot:
        store = get_object_store()
        for role in screenshot_service.IMAGE_ROLES:
            upload = screenshot_service.presign_screenshot(
                db_session, actor_for(member), project=project, content_type="image/png", store=store, role=role
            )
            store.put(upload.key, content_type="image/png", size_bytes=1024, content=PNG_SIGNATURE)
            screenshot_service.confirm_screenshot(
                db_session, actor_for(member), project=project, key=upload.key, store=store, role=role
            )
    project_service.submit_completion(db_session, actor_for(member), project=project)
    project_service.record_review(
        db_session,
        actor_for(reviewer),
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave_stub,
    )
    db_session.refresh(project)
    return project


def test_gallery_index_is_public(client: TestClient) -> None:
    response = client.get("/gallery")

    assert response.status_code == 200


def test_gallery_index_shows_only_completed_projects(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    # Approved, but not yet completed -- should NOT show up in the gallery.
    approved_project(member, reviewer, title="Not Done Yet")

    response = client.get("/gallery")

    assert response.status_code == 200
    assert "Not Done Yet" not in response.text


def test_gallery_detail_shows_a_completed_project(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(db_session, approved_project, member, reviewer, weave_stub=weave_stub)

    index_response = client.get("/gallery")
    assert "Gallery Project" in index_response.text

    detail_response = client.get(f"/gallery/{project.id}")
    assert detail_response.status_code == 200
    assert "Gallery Project" in detail_response.text
    assert "ganymede" in detail_response.text
    assert "example.com/demo" in detail_response.text
    # The team, by the names Weave has for them now.
    assert "Built by" in detail_response.text and "Mia Member" in detail_response.text


def test_gallery_detail_renders_the_write_up_as_markdown(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(
        db_session,
        approved_project,
        member,
        reviewer,
        weave_stub=weave_stub,
        write_up="# Results\n\nThe rover **drove**.\n\n- [code](https://example.com/repo)\n\n<script>alert(1)</script>",
    )

    response = client.get(f"/gallery/{project.id}")

    assert response.status_code == 200
    assert "<h3>Results</h3>" in response.text
    assert "The rover <strong>drove</strong>." in response.text
    assert 'href="https://example.com/repo" rel="nofollow ugc noopener noreferrer"' in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_gallery_detail_404s_for_a_non_completed_project(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    response = client.get(f"/gallery/{project.id}")

    assert response.status_code == 404


def test_gallery_detail_404s_for_an_unknown_project(client: TestClient) -> None:
    response = client.get(f"/gallery/{uuid.uuid4()}")

    assert response.status_code == 404


def test_gallery_shows_the_primary_image_as_a_presigned_url(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(
        db_session, approved_project, member, reviewer, with_screenshot=True, weave_stub=weave_stub
    )
    key = project.current_revision.screenshot_keys[0]

    index_response = client.get("/gallery")
    assert 'class="gallery-list__thumb"' in index_response.text
    assert key in index_response.text  # the fake store's presigned URL embeds the key

    detail_response = client.get(f"/gallery/{project.id}")
    assert detail_response.status_code == 200
    assert 'class="gallery-hero"' in detail_response.text
    assert key in detail_response.text
    # Only the primary image: the inline one shows only where the write-up puts it.
    assert project.current_revision.image_keys[0] not in detail_response.text


def test_a_completed_projects_inline_image_is_public(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(
        db_session, approved_project, member, reviewer, with_screenshot=True, weave_stub=weave_stub
    )
    key = project.current_revision.image_keys[0]
    client.cookies.clear()  # signed out

    response = client.get(screenshot_service.image_path(project.id, key), follow_redirects=False)

    assert response.status_code == 307
    assert key in response.headers["location"]
    assert response.headers["cache-control"] == "private, max-age=600"
    # The primary image isn't served by name: it isn't one of the write-up's.
    primary = project.current_revision.screenshot_keys[0]
    assert client.get(screenshot_service.image_path(project.id, primary), follow_redirects=False).status_code == 404


@pytest.mark.parametrize(
    ("setting", "shown", "hidden"),
    [
        ("full", ["Mia Member"], []),
        ("first", ["<li>Mia</li>"], ["Mia Member"]),
        ("hidden", ["<li>1 team member</li>"], ["Mia"]),
    ],
)
def test_the_gallery_names_a_minor_as_the_admins_chose(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub, setting, shown, hidden
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(db_session, approved_project, member, reviewer, weave_stub=weave_stub)
    weave_stub.set_minor(MEMBER_SUB, True)
    site_settings.current(db_session).gallery_minor_names = GalleryMinorNames(setting)
    db_session.flush()

    page = client.get(f"/gallery/{project.id}").text

    assert all(text in page for text in shown)
    assert not any(text in page for text in hidden)


def test_unknown_age_counts_as_under_18_in_the_gallery(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(db_session, approved_project, member, reviewer, weave_stub=weave_stub)
    weave_stub.set_minor(MEMBER_SUB, None)

    page = client.get(f"/gallery/{project.id}").text

    assert "<li>Mia</li>" in page and "Mia Member" not in page


@pytest.mark.parametrize("setting", ["full", "first"])
def test_a_member_weave_no_longer_returns_is_never_shown_by_patchwork_id(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub, setting
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(db_session, approved_project, member, reviewer, weave_stub=weave_stub)
    site_settings.current(db_session).gallery_minor_names = GalleryMinorNames(setting)
    db_session.flush()
    weave_stub.remove_user(MEMBER_SUB)  # e.g. a guardian withdrew consent: the directory 404s

    page = client.get(f"/gallery/{project.id}").text

    assert MEMBER_SUB not in page and "1 team member" in page
