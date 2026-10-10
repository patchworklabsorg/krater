"""HTTP-level tests for /projects/*: creation, viewing, editing, and the contextual action forms.

The service layer's rules are already covered in tests/services; these focus on routing, auth wiring,
CSRF, and how domain errors map to responses.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import ProjectStatus
from krater.services import projects as project_service
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token

# --------------------------------------------------------------------------------------------------
# /projects/new
# --------------------------------------------------------------------------------------------------


def test_get_new_project_form_requires_sign_in(client: TestClient) -> None:
    response = client.get("/projects/new", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_member_who_lost_ganymede_membership_cannot_create(client: TestClient, login_as, revoke_membership) -> None:
    """Krater's `/login` already refuses a non-member outright, so the case this route must guard
    against is a *previously* signed-in member whose live Weave groups no longer include
    `ganymede:member` -- exactly what `fresh_actor` re-checks on every request."""
    member = login_as(MEMBER_SUB)
    revoke_membership(member)

    response = client.get("/projects/new")

    assert response.status_code == 403


def test_member_can_create_a_project(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "repo_url": "https://github.com/x/rover",
            "write_up": "A rover.",
            "budget_requested": "1,234.50",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    project_id = uuid.UUID(response.headers["location"].removeprefix("/projects/"))
    project = project_service.get_project(db_session, project_id=project_id)
    assert project.title == "Rover"
    assert project.current_revision.budget_requested_cents == 123_450
    assert project.status is ProjectStatus.DRAFT


def test_create_project_rejects_an_invalid_budget(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/projects/new",
        data={"csrf_token": csrf, "title": "X", "write_up": "Y", "budget_requested": "not-a-number"},
    )

    assert response.status_code == 422
    assert "valid dollar amount" in response.text


def test_create_project_without_csrf_fails(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    response = client.post("/projects/new", data={"title": "X", "write_up": "Y", "budget_requested": "10"})

    assert response.status_code == 403


# --------------------------------------------------------------------------------------------------
# /projects/{id}: visibility
# --------------------------------------------------------------------------------------------------


def test_submitter_can_view_their_own_project(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Mine")

    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Mine" in response.text


def test_member_cannot_view_another_members_project(client: TestClient, login_as, create_project) -> None:
    other = login_as(OTHER_MEMBER_SUB)
    other_project = create_project(other, title="Theirs")

    login_as(MEMBER_SUB)
    response = client.get(f"/projects/{other_project.id}")

    assert response.status_code == 404


def test_reviewer_can_view_any_project(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Reviewable")

    login_as(REVIEWER_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Reviewable" in response.text


def test_admin_can_view_any_project(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Adminable")

    login_as(ADMIN_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200


def test_unknown_project_is_404(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    response = client.get(f"/projects/{uuid.uuid4()}")

    assert response.status_code == 404


# --------------------------------------------------------------------------------------------------
# /projects/{id}/edit
# --------------------------------------------------------------------------------------------------


def test_submitter_can_edit_and_save_their_draft(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Original")

    edit_form = client.get(f"/projects/{project.id}/edit")
    assert edit_form.status_code == 200
    csrf = get_csrf_token(edit_form.text)

    response = client.post(
        f"/projects/{project.id}/edit",
        data={
            "csrf_token": csrf,
            "title": "Updated",
            "repo_url": "",
            "write_up": "Updated write-up.",
            "budget_requested": "500",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.title == "Updated"
    assert project.current_revision.budget_requested_cents == 50_000


def test_reviewer_cannot_edit_someone_elses_draft(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)

    login_as(REVIEWER_SUB)
    response = client.get(f"/projects/{project.id}/edit")

    assert response.status_code == 403


def test_edit_rejects_an_unknown_credited_builder_email(client: TestClient, login_as, approved_project) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = approved_project(member, reviewer)
    login_as(MEMBER_SUB)

    completion_start = client.post(
        f"/projects/{project.id}/complete",
        data={"csrf_token": get_csrf_token(client.get(f"/projects/{project.id}").text)},
        follow_redirects=False,
    )
    assert completion_start.status_code == 303

    edit_form = client.get(f"/projects/{project.id}/edit")
    csrf = get_csrf_token(edit_form.text)
    response = client.post(
        f"/projects/{project.id}/edit",
        data={
            "csrf_token": csrf,
            "title": project.title,
            "write_up": "Final write-up.",
            "budget_requested": "100",
            "demo_url": "",
            "tags": "",
            "credited_builder_emails": "nobody-like-this@example.com",
        },
    )

    assert response.status_code == 422
    assert "Unknown email" in response.text


# --------------------------------------------------------------------------------------------------
# submit / amend / complete / withdraw
# --------------------------------------------------------------------------------------------------


def test_submit_happy_path(client: TestClient, login_as, create_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="T", write_up="W", budget_requested_cents=100)

    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(f"/projects/{project.id}/submit", data={"csrf_token": csrf}, follow_redirects=False)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_submit_with_missing_fields_rerenders_the_edit_form(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="", write_up="", budget_requested_cents=0)

    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(f"/projects/{project.id}/submit", data={"csrf_token": csrf})

    assert response.status_code == 422
    assert "required" in response.text.lower()


def test_submit_without_csrf_fails(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)

    response = client.post(f"/projects/{project.id}/submit", data={})

    assert response.status_code == 403


def test_amend_then_complete_then_withdraw_are_hidden_and_gated_correctly(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = approved_project(member, reviewer)

    login_as(MEMBER_SUB)
    detail = client.get(f"/projects/{project.id}")
    assert "Start amendment" in detail.text
    assert "Start completion" in detail.text

    csrf = get_csrf_token(detail.text)
    amend_response = client.post(f"/projects/{project.id}/amend", data={"csrf_token": csrf}, follow_redirects=False)
    assert amend_response.status_code == 303
    assert amend_response.headers["location"] == f"/projects/{project.id}/edit"

    # A second amend attempt while a draft is already in progress is an InvalidState -> flash+redirect.
    second_attempt = client.post(f"/projects/{project.id}/amend", data={"csrf_token": csrf}, follow_redirects=False)
    assert second_attempt.status_code == 303
    assert second_attempt.headers["location"] == f"/projects/{project.id}"
    followed = client.get(second_attempt.headers["location"])
    assert "already has a draft" in followed.text

    withdraw_csrf = get_csrf_token(client.get(f"/projects/{project.id}").text)
    withdraw_response = client.post(
        f"/projects/{project.id}/withdraw", data={"csrf_token": withdraw_csrf}, follow_redirects=False
    )
    assert withdraw_response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN


# --------------------------------------------------------------------------------------------------
# Reviewer action: /projects/{id}/review
# --------------------------------------------------------------------------------------------------


def test_reviewer_can_approve(client: TestClient, login_as, submitted_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(
        f"/projects/{project.id}/review", data={"csrf_token": csrf, "decision": "approve"}, follow_redirects=False
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED


def test_reject_without_a_reason_rerenders_with_a_field_error(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(f"/projects/{project.id}/review", data={"csrf_token": csrf, "decision": "reject"})

    assert response.status_code == 422
    assert "reason" in response.text.lower()


def test_non_reviewer_cannot_record_a_review(client: TestClient, login_as, submitted_project) -> None:
    """The submitter here is a plain member (not a reviewer at all)."""
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(f"/projects/{project.id}/review", data={"csrf_token": csrf, "decision": "approve"})

    assert response.status_code == 403


def test_submitter_who_is_a_reviewer_cannot_approve_their_own_project(
    client: TestClient, login_as, submitted_project
) -> None:
    """`REVIEWER_SUB` is both a member and a reviewer -- submitting their own project must still be
    blocked by the self-review rule specifically (not just the "not a reviewer" rule)."""
    reviewer = login_as(REVIEWER_SUB)
    project = submitted_project(reviewer)

    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(f"/projects/{project.id}/review", data={"csrf_token": csrf, "decision": "approve"})

    assert response.status_code == 403


def test_review_without_csrf_fails(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    login_as(REVIEWER_SUB)
    response = client.post(f"/projects/{project.id}/review", data={"decision": "approve"})

    assert response.status_code == 403


# --------------------------------------------------------------------------------------------------
# Admin actions
# --------------------------------------------------------------------------------------------------


def test_admin_can_decide_and_adjust_budget(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    login_as(ADMIN_SUB)
    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    decide_response = client.post(
        f"/projects/{project.id}/admin-decide",
        data={"csrf_token": csrf, "decision": "approve", "reason": "Looks good."},
        follow_redirects=False,
    )
    assert decide_response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED

    budget_csrf = get_csrf_token(client.get(f"/projects/{project.id}").text)
    budget_response = client.post(
        f"/projects/{project.id}/admin-budget",
        data={"csrf_token": budget_csrf, "amount": "50.00", "reason": "Bonus credit."},
        follow_redirects=False,
    )
    assert budget_response.status_code == 303
    from krater.services import budget as budget_service

    assert budget_service.ceiling_cents(db_session, project) == 15_000


def test_non_admin_cannot_adjust_budget(client: TestClient, login_as, approved_project) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = approved_project(member, reviewer)

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(
        f"/projects/{project.id}/admin-budget", data={"csrf_token": csrf, "amount": "50.00", "reason": "x"}
    )

    assert response.status_code == 403


def test_admin_budget_without_csrf_fails(client: TestClient, login_as, approved_project) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = approved_project(member, reviewer)

    login_as(ADMIN_SUB)
    response = client.post(f"/projects/{project.id}/admin-budget", data={"amount": "50.00", "reason": "x"})

    assert response.status_code == 403


def test_admin_reclaim_requires_a_reason(client: TestClient, login_as, approved_project) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = approved_project(member, reviewer)

    login_as(ADMIN_SUB)
    page = client.get(f"/projects/{project.id}")
    csrf = get_csrf_token(page.text)
    response = client.post(
        f"/projects/{project.id}/admin-reclaim", data={"csrf_token": csrf, "amount": "10.00", "reason": ""}
    )

    assert response.status_code == 422
    assert "reason" in response.text.lower()


# --------------------------------------------------------------------------------------------------
# Bad links and out-of-range budgets come back as field errors, never a 500
# --------------------------------------------------------------------------------------------------


def test_create_project_rejects_a_non_http_repo_link_as_a_field_error(
    client: TestClient, login_as, db_session: Session
) -> None:
    member = login_as(MEMBER_SUB)
    csrf = get_csrf_token(client.get("/projects/new").text)

    response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Keep me",
            "repo_url": "ssh://git@example.com/x.git",
            "write_up": "Typed text that must survive.",
            "budget_requested": "10",
        },
    )

    assert response.status_code == 422
    assert "Enter a full http:// or https:// URL." in response.text
    assert "Typed text that must survive." in response.text
    assert project_service.list_projects_for_user(db_session, user_id=member.id) == []


def test_edit_rejects_a_non_http_repo_link_as_a_field_error(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)
    csrf = get_csrf_token(client.get(f"/projects/{project.id}/edit").text)

    response = client.post(
        f"/projects/{project.id}/edit",
        data={
            "csrf_token": csrf,
            "title": "T",
            "repo_url": "javascript:alert(1)",
            "write_up": "W",
            "budget_requested": "1",
        },
    )

    assert response.status_code == 422
    assert "Enter a full http:// or https:// URL." in response.text


def test_create_project_rejects_a_budget_past_the_maximum(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)
    csrf = get_csrf_token(client.get("/projects/new").text)

    response = client.post(
        "/projects/new",
        data={"csrf_token": csrf, "title": "Big", "write_up": "W", "budget_requested": "21,474,836.48"},
    )

    assert response.status_code == 422
    assert "Enter an amount up to $1,000,000.00." in response.text
