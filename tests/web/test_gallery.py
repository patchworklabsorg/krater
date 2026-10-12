"""HTTP-level tests for the public gallery: /gallery, /gallery/{id}. No auth required."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import ReviewDecision, ReviewSource
from krater.services import projects as project_service
from krater.services import screenshots as screenshot_service
from krater.storage import get_object_store
from tests.conftest import MEMBER_SUB, REVIEWER_SUB
from tests.web.conftest import PNG_SIGNATURE, actor_for


def _completed_project(db_session: Session, approved_project, member, reviewer, *, with_screenshot: bool = False):
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
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(db_session, approved_project, member, reviewer)

    index_response = client.get("/gallery")
    assert "Gallery Project" in index_response.text

    detail_response = client.get(f"/gallery/{project.id}")
    assert detail_response.status_code == 200
    assert "Gallery Project" in detail_response.text
    assert "ganymede" in detail_response.text
    assert "example.com/demo" in detail_response.text


def test_gallery_detail_404s_for_a_non_completed_project(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    response = client.get(f"/gallery/{project.id}")

    assert response.status_code == 404


def test_gallery_detail_404s_for_an_unknown_project(client: TestClient) -> None:
    response = client.get(f"/gallery/{uuid.uuid4()}")

    assert response.status_code == 404


def test_gallery_shows_screenshots_as_presigned_urls(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = _completed_project(db_session, approved_project, member, reviewer, with_screenshot=True)
    key = project.current_revision.screenshot_keys[0]

    index_response = client.get("/gallery")
    assert 'class="gallery-list__thumb"' in index_response.text
    assert key in index_response.text  # the fake store's presigned URL embeds the key

    detail_response = client.get(f"/gallery/{project.id}")
    assert detail_response.status_code == 200
    assert "gallery-screenshots" in detail_response.text
    assert key in detail_response.text
    assert 'alt="Gallery Project screenshot"' in detail_response.text
