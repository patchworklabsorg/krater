"""HTTP-level tests for the admin overview page, /admin."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.models import ApprovalPolicy, ApprovalStage, QuiltOutbox, QuiltOutboxState
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token


def test_admin_page_requires_sign_in(client: TestClient) -> None:
    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_non_admin_gets_403(client: TestClient, login_as) -> None:
    login_as(REVIEWER_SUB)

    response = client.get("/admin")

    assert response.status_code == 403


def test_admin_sees_projects_grouped_by_status_and_the_default_policy_note(
    client: TestClient, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    submitted_project(member, title="Pending One")

    login_as(ADMIN_SUB)
    response = client.get("/admin")

    assert response.status_code == 200
    assert "Pending One" in response.text
    assert "No custom policies configured" in response.text


def test_admin_page_lists_configured_policies(client: TestClient, login_as, db_session) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    db_session.flush()

    login_as(ADMIN_SUB)
    response = client.get("/admin")

    assert response.status_code == 200
    assert "No custom policies configured" not in response.text
    assert "Proposal" in response.text


# --------------------------------------------------------------------------------------------------
# Delivery to Quilt
# --------------------------------------------------------------------------------------------------


def _failed_quilt_row(db_session, project) -> QuiltOutbox:
    row = db_session.scalars(sa.select(QuiltOutbox).where(QuiltOutbox.external_id == str(project.id))).one()
    row.state = QuiltOutboxState.FAILED
    row.last_status = 409
    row.last_error = "submission_exists"
    db_session.flush()
    return row


def test_admin_page_lists_events_quilt_refused(client: TestClient, login_as, submitted_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    row = _failed_quilt_row(db_session, submitted_project(member, title="Refused One"))

    login_as(ADMIN_SUB)
    response = client.get("/admin")

    assert response.status_code == 200
    assert "Delivery to Quilt" in response.text
    assert "Refused by Quilt" in response.text
    assert str(row.id) in response.text
    assert "submission_exists" in response.text


def test_admin_can_retry_a_refused_event(client: TestClient, login_as, submitted_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    row = _failed_quilt_row(db_session, submitted_project(member))
    login_as(ADMIN_SUB)
    csrf = get_csrf_token(client.get("/admin").text)

    response = client.post(
        f"/admin/quilt/{row.id}/retry", data={"csrf_token": csrf, "reason": "Fixed."}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin#quilt"
    db_session.refresh(row)
    assert row.state is QuiltOutboxState.PENDING


def test_admin_can_dismiss_a_refused_event(client: TestClient, login_as, submitted_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    row = _failed_quilt_row(db_session, submitted_project(member))
    login_as(ADMIN_SUB)
    csrf = get_csrf_token(client.get("/admin").text)

    response = client.post(
        f"/admin/quilt/{row.id}/dismiss", data={"csrf_token": csrf, "reason": "Not needed."}, follow_redirects=False
    )

    assert response.status_code == 303
    db_session.refresh(row)
    assert row.state is QuiltOutboxState.SKIPPED


def test_a_quilt_action_without_a_reason_changes_nothing(
    client: TestClient, login_as, submitted_project, db_session
) -> None:
    member = login_as(MEMBER_SUB)
    row = _failed_quilt_row(db_session, submitted_project(member))
    login_as(ADMIN_SUB)
    csrf = get_csrf_token(client.get("/admin").text)

    response = client.post(f"/admin/quilt/{row.id}/retry", data={"csrf_token": csrf, "reason": ""})

    assert response.status_code == 200  # back on /admin, with the error flashed
    assert "A reason is required" in response.text
    db_session.refresh(row)
    assert row.state is QuiltOutboxState.FAILED


def test_a_non_admin_cannot_retry_or_dismiss(client: TestClient, login_as, submitted_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    row = _failed_quilt_row(db_session, submitted_project(member))
    login_as(REVIEWER_SUB)
    csrf = get_csrf_token(client.get("/projects/new").text)

    for action in ("retry", "dismiss"):
        response = client.post(f"/admin/quilt/{row.id}/{action}", data={"csrf_token": csrf, "reason": "x"})
        assert response.status_code == 403
    db_session.refresh(row)
    assert row.state is QuiltOutboxState.FAILED


def test_quilt_actions_need_a_csrf_token(client: TestClient, login_as, submitted_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    row = _failed_quilt_row(db_session, submitted_project(member))
    login_as(ADMIN_SUB)

    response = client.post(f"/admin/quilt/{row.id}/retry", data={"reason": "x"})

    assert response.status_code == 403
