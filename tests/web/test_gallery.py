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
    db_session: Session, approved_project, member, reviewer, weave_stub, *, with_screenshot: bool = False
):
    project = approved_project(member, reviewer, title="Gallery Project")
    project_service.start_completion(db_session, actor_for(member), project=project)
    project_service.update_draft(
        db_session,
        actor_for(member),
        project=project,
        demo_url="https://example.com/demo",
        tags=["ganymede", "rover"],
    )
    if with_screenshot:
        store = get_object_store()
        upload = screenshot_service.presign_screenshot(
            db_session, actor_for(member), project=project, content_type="image/png", store=store
        )
        store.put(upload.key, content_type="image/png", size_bytes=1024, content=PNG_SIGNATURE)
        screenshot_service.confirm_screenshot(
            db_session, actor_for(member), project=project, key=upload.key, store=store
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


def test_gallery_detail_404s_for_a_non_completed_project(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    response = client.get(f"/gallery/{project.id}")

    assert response.status_code == 404


def test_gallery_detail_404s_for_an_unknown_project(client: TestClient) -> None:
    response = client.get(f"/gallery/{uuid.uuid4()}")

    assert response.status_code == 404


def test_gallery_shows_screenshots_as_presigned_urls(
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
    assert "gallery-screenshots" in detail_response.text
    assert key in detail_response.text
    assert 'alt="Gallery Project screenshot"' in detail_response.text


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
