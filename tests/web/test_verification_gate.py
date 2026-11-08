"""HTTP-level tests for verification before funding: the web approve paths approve a project whatever the team's
verification in Weave, and the project page says how much budget is held and whose verification it's waiting on. The
rules themselves are covered in tests/services/test_clearance.py and tests/services/test_grants.py."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import ProjectStatus
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token

# Fixture users from krater/weave/stub_users.json: both under 18, one with a guardian Weave has verified.
MINOR_SUB = "PWLMINORONE"
VERIFIED_MINOR_SUB = "PWLMINORVERIFIED"


def _post_decision(client: TestClient, project, path: str, **data: str):
    csrf = get_csrf_token(client.get(f"/projects/{project.id}").text)
    return client.post(f"/projects/{project.id}/{path}", data={"csrf_token": csrf, **data}, follow_redirects=False)


def test_a_reviewer_approves_a_minors_project_and_the_page_says_its_budget_is_held(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    project = submitted_project(login_as(MINOR_SUB))

    login_as(REVIEWER_SUB)
    response = _post_decision(client, project, "review", decision="approve")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    page = client.get(f"/projects/{project.id}").text
    assert "Approved. Its budget of $" in page and "is held until every team member is verified in Weave" in page
    held = project.approved_revision.budget_requested_cents / 100
    assert f"{held:,.2f} of this project's budget is held until every team member" in page
    assert "Milo Minor (PWLMINORONE): under 18 without a verified guardian in Weave" in page


def test_an_admin_increase_says_it_is_held(
    client: TestClient, login_as, submitted_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    project = submitted_project(login_as(MEMBER_SUB))
    login_as(REVIEWER_SUB)
    _post_decision(client, project, "review", decision="approve")
    weave_stub.set_verification_status(MEMBER_SUB, "pending")

    login_as(ADMIN_SUB)
    response = _post_decision(client, project, "admin-budget", amount="25", reason="More.")

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Budget increase held until every team member is verified in Weave." in page
    assert "25.00 of this project&#39;s budget is held" in page or "25.00 of this project's budget is held" in page


def test_an_admin_approval_goes_through_too(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    project = submitted_project(login_as(MINOR_SUB))

    login_as(ADMIN_SUB)
    response = _post_decision(client, project, "admin-decide", decision="approve", reason="Good.")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED


def test_the_project_page_lists_a_member_without_id_verification(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient
) -> None:
    project = submitted_project(login_as(MEMBER_SUB))
    weave_stub.set_verification_status(MEMBER_SUB, "needs_submission")

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")

    assert page.status_code == 200
    assert 'class="guardian-banner"' in page.text
    assert "Mia Member (PWLMEMBERONE): hasn&#39;t done ID verification in Weave" in page.text
    assert "budget and compute are held until every team member is verified in Weave" in page.text
    assert "Weave's verification page" in page.text


def test_the_project_page_has_no_notice_once_the_team_is_cleared(
    client: TestClient, login_as, submitted_project
) -> None:
    project = submitted_project(login_as(VERIFIED_MINOR_SUB))

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")

    assert page.status_code == 200
    assert "guardian-banner" not in page.text


def test_the_project_page_says_when_weave_could_not_be_checked(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient, monkeypatch
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    real_get_user = weave_stub.get_user

    def get_user(sub: str, *, fresh: bool = False):
        # The viewer's own role check still works; only the team lookup fails.
        if sub == member.weave_sub:
            raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    login_as(REVIEWER_SUB)
    monkeypatch.setattr(weave_stub, "get_user", get_user)
    page = client.get(f"/projects/{project.id}")

    assert page.status_code == 200
    assert "couldn't reach Weave to check that the team is verified" in page.text


def test_a_member_weave_does_not_return_is_listed(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient
) -> None:
    project = submitted_project(login_as(MEMBER_SUB))
    weave_stub.remove_user(MEMBER_SUB)

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")

    assert "PWLMEMBERONE: not available in Weave (no access to Krater)" in page.text


# -- "Awaiting verification" -------------------------------------------------------------------------------------


def test_a_minor_submits_and_is_approved_and_every_list_says_awaiting_verification(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    from datetime import UTC, datetime

    from krater.services import budget, grants

    minor = login_as(MINOR_SUB)
    project = create_project(minor, title="Minor rover")
    csrf = get_csrf_token(client.get(f"/projects/{project.id}").text)
    submitted = client.post(
        f"/projects/{project.id}/submit", data={"csrf_token": csrf, "criteria_read": "yes"}, follow_redirects=False
    )
    assert submitted.status_code == 303  # no guardian needed to submit
    login_as(REVIEWER_SUB)
    _post_decision(client, project, "review", decision="approve")
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 0  # no funding yet

    assert "Approved · awaiting verification" in client.get(f"/projects/{project.id}").text
    login_as(ADMIN_SUB)
    assert "Approved · awaiting verification" in client.get("/admin").text
    login_as(MINOR_SUB)
    assert "Approved · awaiting verification" in client.get("/").text

    weave_stub.set_guardian_verified(MINOR_SUB, True)
    assert grants.release_ready(db_session, weave_stub, now=datetime.now(UTC)) == 1
    assert "Approved · awaiting verification" not in client.get("/").text
    assert "awaiting verification" not in client.get(f"/projects/{project.id}").text


def test_a_verified_teams_approval_has_no_flag(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    project = approved_project(login_as(MEMBER_SUB), login_as(REVIEWER_SUB))

    page = client.get(f"/projects/{project.id}").text

    assert "awaiting verification" not in page
