"""The web side of the Submission Criteria box, the training data question and funding holds."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import AuditEvent, FundingHold, ProjectStatus
from krater.services import budget, funding_holds, grants
from krater.services.actor import Actor
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.services.conftest import add_to_team
from tests.web.test_application_form import _create, _revision, _seed_price


def _post(client: TestClient, url: str, page_url: str, **data):
    csrf = get_csrf_token(client.get(page_url).text)
    return client.post(url, data={"csrf_token": csrf, **data}, follow_redirects=False)


# -- Submission Criteria and training data ---------------------------------------------------------


def test_the_edit_page_asks_for_the_criteria_and_the_training_data(client: TestClient, login_as, create_project):
    member = login_as(MEMBER_SUB)
    project = create_project(member)

    page = client.get(f"/projects/{project.id}/edit").text

    assert 'name="criteria_read"' in page
    assert "docs.google.com/document/d/16mQKrPm1LQabLe05OXBT9vmwiACDw0YGMTt6QOVG-Pk" in page
    assert "Where does your training data come from?" in page
    assert "Cave scans we took ourselves." in page


def test_submitting_without_the_box_is_refused(client: TestClient, login_as, create_project, db_session: Session):
    member = login_as(MEMBER_SUB)
    project = create_project(member)
    db_session.commit()  # the refused submit rolls back to here

    response = _post(client, f"/projects/{project.id}/submit", f"/projects/{project.id}")

    assert response.status_code == 422
    assert "Read the Ganymede Submission Criteria, then tick the box." in response.text
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT


def test_the_project_page_shows_the_training_data(client: TestClient, login_as, submitted_project):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text

    assert "Where their training data comes from" in page
    assert "Cave scans we took ourselves." in page


def test_the_form_saves_the_training_data(client: TestClient, login_as, db_session: Session):
    login_as(MEMBER_SUB)
    _seed_price(db_session)

    response = _create(client, training_data="The Open Caves dataset.")

    assert _revision(db_session, response).training_data == "The Open Caves dataset."


# -- Approving with a hold ------------------------------------------------------------------------


def test_a_reviewer_can_approve_as_awaiting_funding(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text
    assert "approve as awaiting funding" in page

    response = _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        funding_hold="awaiting_funding",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert project.funding_hold is FundingHold.AWAITING_FUNDING
    assert budget.ceiling_cents(db_session, project) == 0
    page = client.get(f"/projects/{project.id}").text
    assert "Approved · awaiting funding" in page
    assert "Approved as awaiting funding." in page  # the flash
    login_as(MEMBER_SUB)
    assert "Approved, and waiting on Patchwork to fund it." in client.get(f"/projects/{project.id}").text
    assert "Approved · awaiting funding" in client.get("/").text


def test_an_unknown_hold_is_refused(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    login_as(REVIEWER_SUB)

    response = _post(
        client, f"/projects/{project.id}/review", f"/projects/{project.id}", decision="approve", funding_hold="later"
    )

    assert response.status_code == 422
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_an_admin_can_approve_as_awaiting_a_3rd_party_review(client, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-decide",
        f"/projects/{project.id}",
        decision="approve",
        reason="Board said yes.",
        funding_hold="awaiting_third_party_review",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.funding_hold is FundingHold.AWAITING_THIRD_PARTY_REVIEW
    admin_page = client.get("/admin").text
    assert "Approved · awaiting 3rd party review" in admin_page
    assert "An admin clears it on the project's page." in admin_page


# -- Setting and clearing it later ----------------------------------------------------------------


def test_an_admin_clears_a_hold_and_the_budget_is_added(client, login_as, submitted_project, db_session, weave_stub):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    login_as(REVIEWER_SUB)
    _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        funding_hold="awaiting_funding",
    )
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-funding-hold",
        f"/projects/{project.id}",
        funding_hold="",
        reason="The grant came through.",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.funding_hold is None
    assert budget.ceiling_cents(db_session, project) == project.current_revision.budget_requested_cents
    assert grants.pending(db_session, project) is None
    assert "Hold cleared. The held budget is added." in client.get(f"/projects/{project.id}").text
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == funding_holds.AUDIT_CLEAR))
    assert event.reason == "The grant came through."


def test_an_admin_can_hold_an_approved_project(client, login_as, approved_project, db_session):
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-funding-hold",
        f"/projects/{project.id}",
        funding_hold="awaiting_third_party_review",
        reason="Sent to OSPool.",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.funding_hold is FundingHold.AWAITING_THIRD_PARTY_REVIEW


def test_a_hold_needs_a_reason(client, login_as, approved_project, db_session):
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-funding-hold",
        f"/projects/{project.id}",
        funding_hold="awaiting_funding",
        reason="",
    )

    assert response.status_code == 422
    assert "A reason is required." in response.text
    db_session.refresh(project)
    assert project.funding_hold is None


def test_members_and_reviewers_cant_set_a_hold(client, login_as, approved_project, db_session):
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))

    for sub in (MEMBER_SUB, REVIEWER_SUB):
        login_as(sub)
        response = _post(
            client,
            f"/projects/{project.id}/admin-funding-hold",
            f"/projects/{project.id}",
            funding_hold="awaiting_funding",
            reason="x",
        )
        assert response.status_code == 403
    db_session.refresh(project)
    assert project.funding_hold is None


def test_an_admin_cant_clear_the_hold_on_their_own_project(client, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    admin = login_as(ADMIN_SUB)
    _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        funding_hold="awaiting_funding",
    )  # an admin is a reviewer too
    db_session.refresh(project)
    assert project.funding_hold is FundingHold.AWAITING_FUNDING
    add_to_team(db_session, project, Actor(user=admin, groups=frozenset()))
    db_session.commit()

    page = client.get(f"/projects/{project.id}").text
    assert "No hold (clear it)" not in page
    response = _post(
        client, f"/projects/{project.id}/admin-funding-hold", f"/projects/{project.id}", funding_hold="", reason="x"
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.funding_hold is FundingHold.AWAITING_FUNDING
    assert "Ask another admin." in client.get(f"/projects/{project.id}").text


def test_an_admin_increase_on_a_held_project_says_the_hold_holds_it(client, login_as, approved_project, db_session):
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    login_as(ADMIN_SUB)
    page = f"/projects/{project.id}"
    _post(client, f"{page}/admin-funding-hold", page, funding_hold="awaiting_funding", reason="Waiting on the grant.")

    response = _post(client, f"{page}/admin-budget", page, amount="25", reason="More.")

    assert response.status_code == 303
    shown = client.get(response.headers["location"]).text
    assert "Budget increase held: the project is awaiting funding" in shown
    assert "added once an admin clears the hold" in shown
    assert "Budget increase held until every team member is verified" not in shown
    db_session.refresh(project)
    assert grants.pending(db_session, project).amount_cents == 2500


def test_the_activity_feed_shows_holds_being_set_and_cleared(client, login_as, approved_project, db_session):
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    login_as(ADMIN_SUB)
    page = f"/projects/{project.id}"
    _post(client, f"{page}/admin-funding-hold", page, funding_hold="awaiting_funding", reason="Waiting on the grant.")
    _post(client, f"{page}/admin-funding-hold", page, funding_hold="", reason="Grant arrived.")

    for url in ("/admin/activity", f"/admin/activity?project={project.id}"):
        response = client.get(url)
        assert response.status_code == 200
        assert "Admin set funding hold" in response.text
        assert "Admin clear funding hold" in response.text
        assert "Grant arrived." in response.text


def test_a_held_projects_compute_page_offers_no_launch(client, login_as, approved_project, db_session, monkeypatch):
    from krater.config import get_settings

    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    login_as(MEMBER_SUB)
    project.funding_hold = FundingHold.AWAITING_FUNDING
    db_session.commit()

    page = client.get(f"/projects/{project.id}/compute").text
    project.funding_hold = None
    db_session.commit()
    unheld = client.get(f"/projects/{project.id}/compute").text

    assert "New job or box" not in page
    assert "New job or box" in unheld
