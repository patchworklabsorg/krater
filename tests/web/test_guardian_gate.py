"""HTTP-level tests for the guardian gate: the web approve paths refuse a grant while a team member is a minor
without a verified guardian in Weave, and the project page says who needs one. The rule itself is covered in
tests/services/test_guardian.py."""

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


def test_a_reviewer_approval_is_refused_and_names_the_member(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    project = submitted_project(login_as(MINOR_SUB))

    login_as(REVIEWER_SUB)
    response = _post_decision(client, project, "review", decision="approve")

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}"
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW
    followed = client.get(response.headers["location"])
    assert "verified guardian in Weave" in followed.text
    assert "Milo Minor (PWLMINORONE)" in followed.text


def test_an_admin_approval_cannot_bypass_the_gate(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    project = submitted_project(login_as(MINOR_SUB))

    login_as(ADMIN_SUB)
    response = _post_decision(client, project, "admin-decide", decision="approve", reason="Urgent.")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_minor_with_a_verified_guardian_can_be_approved(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    project = submitted_project(login_as(VERIFIED_MINOR_SUB))

    login_as(REVIEWER_SUB)
    response = _post_decision(client, project, "review", decision="approve")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED


def test_the_project_page_shows_who_needs_a_verified_guardian(client: TestClient, login_as, submitted_project) -> None:
    project = submitted_project(login_as(MINOR_SUB))

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")

    assert page.status_code == 200
    assert 'class="guardian-banner"' in page.text
    assert "Milo Minor (PWLMINORONE)" in page.text
    assert "Weave admin page" in page.text


def test_the_project_page_has_no_notice_without_a_blocked_member(
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
    assert "couldn" in page.text
    assert "check whether a team member needs a verified guardian" in page.text


def test_a_member_weave_does_not_return_is_listed_and_blocks_the_approval(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient, db_session: Session
) -> None:
    project = submitted_project(login_as(MEMBER_SUB))
    weave_stub.remove_user(MEMBER_SUB)

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")
    response = _post_decision(client, project, "review", decision="approve")

    assert "PWLMEMBERONE: not available in Weave (no access to Krater)" in page.text
    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW
