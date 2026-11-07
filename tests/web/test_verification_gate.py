"""HTTP-level tests for verification before compute: the web approve paths approve a project whatever the team's
verification in Weave, and the project page says whose verification its compute is waiting on. The rule itself is
covered in tests/services/test_clearance.py."""

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


def test_a_reviewer_approves_a_minors_project_and_the_page_says_compute_is_held(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    project = submitted_project(login_as(MINOR_SUB))

    login_as(REVIEWER_SUB)
    response = _post_decision(client, project, "review", decision="approve")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    page = client.get(f"/projects/{project.id}").text
    assert "compute is held until every team member is verified in Weave" in page
    assert "Milo Minor (PWLMINORONE): under 18 without a verified guardian in Weave" in page


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
