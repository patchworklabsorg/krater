"""The web side of granting less (or more) than a proposal asks for: the "Amount to grant" field on the approve
forms."""

from __future__ import annotations

from fastapi.testclient import TestClient

from krater.models import ProjectStatus
from krater.services import budget
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token


def _post(client: TestClient, url: str, page_url: str, **data):
    csrf = get_csrf_token(client.get(page_url).text)
    return client.post(url, data={"csrf_token": csrf, **data}, follow_redirects=False)


def test_a_reviewer_grants_less_on_the_web(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text
    assert "Amount to grant (USD)" in page
    assert 'value="180.00"' in page  # starts at the full request

    response = _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        budget="50",
        reason="A smaller first step.",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 5_000
    page = client.get(f"/projects/{project.id}").text
    assert "Approved for $50.00 of the $180.00 asked for." in page  # the flash
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}").text
    assert "Approved for $50.00" in page
    assert "A smaller first step." in page


def test_the_full_amount_needs_no_reason(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    login_as(REVIEWER_SUB)

    response = _post(
        client, f"/projects/{project.id}/review", f"/projects/{project.id}", decision="approve", budget="180.00"
    )

    assert response.status_code == 303
    assert budget.ceiling_cents(db_session, project) == 18_000


def test_less_without_a_reason_is_refused_and_keeps_the_amount(
    client: TestClient, login_as, submitted_project, db_session
):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    db_session.commit()  # the refused review rolls back to here
    login_as(REVIEWER_SUB)

    response = _post(
        client, f"/projects/{project.id}/review", f"/projects/{project.id}", decision="approve", budget="50"
    )

    assert response.status_code == 422
    assert "Say why you&#39;re granting less" in response.text
    assert 'value="50"' in response.text
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_bad_amount_is_refused(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    db_session.commit()
    login_as(REVIEWER_SUB)

    response = _post(
        client, f"/projects/{project.id}/review", f"/projects/{project.id}", decision="approve", budget="lots"
    )

    assert response.status_code == 422
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_an_admin_grants_less_with_the_override(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-decide",
        f"/projects/{project.id}",
        decision="approve",
        reason="Board: $50 to start.",
        budget="50.00",
    )

    assert response.status_code == 303
    assert budget.ceiling_cents(db_session, project) == 5_000


def test_admin_reject_ignores_the_amount(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-decide",
        f"/projects/{project.id}",
        decision="reject",
        reason="Not this round.",
        budget="180.00",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.CHANGES_REQUESTED


def test_a_reviewer_grants_more_on_the_web(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text
    assert "or raise it to fund more" in page

    response = _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        budget="250",
        reason="It needs a bigger card.",
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 25_000
    page = client.get(f"/projects/{project.id}").text
    assert "Approved for $250.00, more than the $180.00 asked for." in page  # the flash
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}").text
    assert "Approved for $250.00" in page
    assert "It needs a bigger card." in page


def test_more_without_a_reason_is_refused_on_the_web(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    db_session.commit()  # the refused review rolls back to here
    login_as(REVIEWER_SUB)

    response = _post(
        client, f"/projects/{project.id}/review", f"/projects/{project.id}", decision="approve", budget="250"
    )

    assert response.status_code == 422
    assert "Say why you&#39;re granting more" in response.text
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_member_cant_grant_more(client: TestClient, login_as, submitted_project, db_session):
    # The team's own lead, a plain member who can see the project page but isn't a reviewer.
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    db_session.commit()

    response = _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        budget="250",
        reason="More, please.",
    )

    assert response.status_code == 403
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW
    assert budget.ceiling_cents(db_session, project) == 0


def test_an_admin_grants_more_with_the_override(client: TestClient, login_as, submitted_project, db_session):
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, budget_requested_cents=18_000)
    login_as(ADMIN_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-decide",
        f"/projects/{project.id}",
        decision="approve",
        reason="Board: fund the bigger card.",
        budget="250.00",
    )

    assert response.status_code == 303
    assert budget.ceiling_cents(db_session, project) == 25_000
