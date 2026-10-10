"""HTTP-level tests for the admin overview page, /admin."""

from __future__ import annotations

from fastapi.testclient import TestClient

from krater.models import ApprovalPolicy, ApprovalStage
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB


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
