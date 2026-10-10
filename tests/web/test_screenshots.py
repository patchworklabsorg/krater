"""HTTP-level tests for /projects/{id}/screenshots/*: presign, confirm, delete.

Uses the process-wide fake `ObjectStore` (`KRATER_S3_MODE=fake` is the test default) directly to
simulate "the browser uploaded to the presigned URL", since `TestClient` can't actually POST to it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.services import projects as project_service
from krater.storage import get_object_store
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import PNG_SIGNATURE, actor_for


def _completion_draft(db_session: Session, approved_project, member, reviewer):
    project = approved_project(member, reviewer)
    project_service.start_completion(db_session, actor_for(member), project=project)
    # Commit (onto the test's SAVEPOINT machinery -- see tests/conftest.py's `db_session` fixture) so a
    # later route-internal `db_session.rollback()` (e.g. a rejected confirm) can't unwind the fixture's
    # own setup along with it.
    db_session.commit()
    db_session.refresh(project)
    return project


def test_presign_confirm_and_display_happy_path(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)  # back to the submitter
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    edit_page = client.get(f"/projects/{project.id}/edit")
    csrf = get_csrf_token(edit_page.text)

    presign_response = client.post(
        f"/projects/{project.id}/screenshots/presign",
        data={"csrf_token": csrf, "content_type": "image/png"},
    )
    assert presign_response.status_code == 200, presign_response.text
    body = presign_response.json()
    key = body["key"]
    assert key.startswith(f"projects/{project.id}/")
    assert body["url"]
    assert "Content-Type" in body["fields"]

    # Simulate the browser's direct upload to storage.
    store = get_object_store()
    store.put(key, content_type="image/png", size_bytes=2048, content=PNG_SIGNATURE)

    confirm_response = client.post(f"/projects/{project.id}/screenshots/confirm", data={"csrf_token": csrf, "key": key})
    assert confirm_response.status_code == 200, confirm_response.text

    db_session.refresh(project)
    assert key in project.current_revision.screenshot_keys

    edit_page_after = client.get(f"/projects/{project.id}/edit")
    assert "screenshot-list__thumb" in edit_page_after.text

    detail_page = client.get(f"/projects/{project.id}")
    assert "Screenshots" in detail_page.text


def test_presign_rejects_a_disallowed_content_type(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    csrf = get_csrf_token(client.get(f"/projects/{project.id}/edit").text)
    response = client.post(
        f"/projects/{project.id}/screenshots/presign", data={"csrf_token": csrf, "content_type": "image/gif"}
    )

    assert response.status_code == 422
    assert "PNG" in response.json()["errors"]["screenshot"]


def test_presign_requires_csrf(client: TestClient, login_as, approved_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    response = client.post(f"/projects/{project.id}/screenshots/presign", data={"content_type": "image/png"})

    assert response.status_code == 403


def test_non_submitter_cannot_presign(client: TestClient, login_as, approved_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    other = login_as(OTHER_MEMBER_SUB)
    del other
    csrf = get_csrf_token(client.get(f"/projects/{project.id}").text)

    response = client.post(
        f"/projects/{project.id}/screenshots/presign", data={"csrf_token": csrf, "content_type": "image/png"}
    )

    # Not the submitter, and not a reviewer/admin either, so the project itself 404s (see
    # `_get_visible_project`) rather than leaking a 403 that would confirm the project exists.
    assert response.status_code == 404


def test_confirm_rejects_an_object_that_is_too_big(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    from krater.services import screenshots as screenshot_service

    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    csrf = get_csrf_token(client.get(f"/projects/{project.id}/edit").text)
    presign = client.post(
        f"/projects/{project.id}/screenshots/presign", data={"csrf_token": csrf, "content_type": "image/png"}
    ).json()

    store = get_object_store()
    store.put(presign["key"], content_type="image/png", size_bytes=screenshot_service.MAX_SCREENSHOT_BYTES + 1)

    response = client.post(
        f"/projects/{project.id}/screenshots/confirm", data={"csrf_token": csrf, "key": presign["key"]}
    )

    assert response.status_code == 422
    db_session.refresh(project)
    assert presign["key"] not in project.current_revision.screenshot_keys


def test_delete_removes_the_screenshot_without_js(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    csrf = get_csrf_token(client.get(f"/projects/{project.id}/edit").text)
    presign = client.post(
        f"/projects/{project.id}/screenshots/presign", data={"csrf_token": csrf, "content_type": "image/png"}
    ).json()
    store = get_object_store()
    store.put(presign["key"], content_type="image/png", size_bytes=100, content=PNG_SIGNATURE)
    client.post(f"/projects/{project.id}/screenshots/confirm", data={"csrf_token": csrf, "key": presign["key"]})

    delete_response = client.post(
        f"/projects/{project.id}/screenshots/{presign['key']}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert delete_response.status_code == 303
    db_session.refresh(project)
    assert presign["key"] not in project.current_revision.screenshot_keys
    assert store.head(presign["key"]) is None


def test_delete_requires_csrf(client: TestClient, login_as, approved_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    reviewer_user = login_as(REVIEWER_SUB)
    login_as(MEMBER_SUB)
    project = _completion_draft(db_session, approved_project, member, reviewer_user)

    response = client.post(f"/projects/{project.id}/screenshots/some/fake/key.png/delete", data={})

    assert response.status_code == 403
