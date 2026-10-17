"""The Team card on a project page, and team invitations on the home page."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import ProjectMember
from krater.web.routers import projects as route_module
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token


def _invite(client: TestClient, project_id, member: str):
    page = client.get(f"/projects/{project_id}")
    return client.post(
        f"/projects/{project_id}/team",
        data={"csrf_token": get_csrf_token(page.text), "member": member},
        follow_redirects=False,
    )


def test_the_lead_invites_by_email_and_the_invitee_joins_from_home(
    client: TestClient, login_as, create_project, db_session: Session, monkeypatch
) -> None:
    lead = login_as(MEMBER_SUB)
    project = create_project(lead, title="Rover")
    db_session.commit()

    assert "Add a teammate" in client.get(f"/projects/{project.id}").text
    response = _invite(client, project.id, "max@example.com")
    assert response.status_code == 303

    page = client.get(f"/projects/{project.id}").text
    assert "Max Member" in page and "Invited" in page

    login_as(OTHER_MEMBER_SUB)
    home = client.get("/")
    assert "Team invitations" in home.text and "Rover" in home.text
    invitation = db_session.scalar(sa.select(ProjectMember).where(ProjectMember.project_id == project.id))
    deferred: dict = {}
    monkeypatch.setattr(route_module.slack_invite_team, "defer", lambda **kwargs: deferred.update(kwargs) or 1)

    joined = client.post(
        f"/projects/{project.id}/team/{invitation.id}/accept",
        data={"csrf_token": get_csrf_token(home.text)},
        follow_redirects=False,
    )

    assert joined.status_code == 303
    assert deferred == {"project_id": str(project.id)}
    db_session.refresh(invitation)
    assert invitation.accepted_at is not None
    page = client.get(f"/projects/{project.id}")
    assert "Edit draft" in page.text and "Leave" in page.text
    assert "Team invitations" not in client.get("/").text


def test_an_unknown_person_redisplays_the_form_with_an_error(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    lead = login_as(MEMBER_SUB)
    project = create_project(lead)
    db_session.commit()

    response = _invite(client, project.id, "nobody@example.com")

    assert response.status_code == 422
    assert "No Ganymede member has that Patchwork ID or email." in response.text
    assert 'value="nobody@example.com"' in response.text


def test_people_with_no_part_in_the_project_cant_see_or_invite(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    lead = login_as(MEMBER_SUB)
    project = create_project(lead)
    db_session.commit()
    login_as(OTHER_MEMBER_SUB)

    assert client.get(f"/projects/{project.id}").status_code == 404
    home = client.get("/")
    response = client.post(
        f"/projects/{project.id}/team",
        data={"csrf_token": get_csrf_token(home.text), "member": "max@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_an_invitee_can_read_the_project_and_answer_from_it(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    lead = login_as(MEMBER_SUB)
    project = create_project(lead)
    db_session.commit()
    _invite(client, project.id, OTHER_MEMBER_SUB)

    login_as(OTHER_MEMBER_SUB)
    page = client.get(f"/projects/{project.id}")

    assert page.status_code == 200
    assert "You're invited" in page.text and "Join the team" in page.text
    assert "Edit draft" not in page.text


def test_a_reviewer_invited_to_a_team_loses_the_review_form(
    client: TestClient, login_as, submitted_project, db_session: Session
) -> None:
    lead = login_as(MEMBER_SUB)
    project = submitted_project(lead)
    db_session.commit()
    _invite(client, project.id, "rae@example.com")

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text

    assert 'action="/projects/' + str(project.id) + '/review"' not in page


def test_an_admin_can_remove_a_member(client: TestClient, login_as, create_project, db_session: Session) -> None:
    lead = login_as(MEMBER_SUB)
    project = create_project(lead)
    db_session.commit()
    _invite(client, project.id, OTHER_MEMBER_SUB)
    invitation = db_session.scalar(sa.select(ProjectMember).where(ProjectMember.project_id == project.id))

    login_as(ADMIN_SUB)
    page = client.get(f"/projects/{project.id}")
    response = client.post(
        f"/projects/{project.id}/team/{invitation.id}/remove",
        data={"csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert db_session.scalar(sa.select(ProjectMember).where(ProjectMember.project_id == project.id)) is None
