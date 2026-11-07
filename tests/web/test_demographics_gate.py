"""HTTP-level tests for the demographics gate: submitting a grant application is refused until the submitter has
answered Weave's demographics form, and the page links to that form. The rule itself is covered in
tests/services/test_demographics.py."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import ProjectStatus
from krater.services import projects as project_service
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.conftest import MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import actor_for

WEAVE_ISSUER = "https://weave.example.test"


@pytest.fixture(autouse=True)
def weave_issuer(monkeypatch) -> None:
    """Stub mode has no issuer; set one so the page has a Weave link to show."""
    monkeypatch.setattr(get_settings(), "weave_issuer", WEAVE_ISSUER)


def _post_submit(client: TestClient, project_id, route: str = "submit"):
    csrf = get_csrf_token(client.get(f"/projects/{project_id}").text)
    return client.post(
        f"/projects/{project_id}/{route}", data={"csrf_token": csrf, "criteria_read": "yes"}, follow_redirects=False
    )


def test_a_member_who_answered_can_submit(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_demographics_answered(MEMBER_SUB, True)
    project = create_project(login_as(MEMBER_SUB), title="T", write_up="W", budget_requested_cents=100)

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_member_who_has_not_answered_is_refused_with_a_link_back(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_demographics_answered(MEMBER_SUB, False)
    project = create_project(login_as(MEMBER_SUB), title="Rover", write_up="A rover.", budget_requested_cents=100)
    db_session.commit()

    response = _post_submit(client, project.id)

    assert response.status_code == 409
    assert "demographics form in Weave" in response.text
    return_to = quote(f"{get_settings().base_url}/projects/{project.id}", safe="")
    assert f'href="{WEAVE_ISSUER}/profile/demographics?return_to={return_to}"' in response.text
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT
    # The draft keeps what the member wrote, and they can submit it once they've answered.
    assert project.title == "Rover"
    assert project.current_revision.submitted_at is None
    assert project.current_revision.write_up == "A rover."

    weave_stub.set_demographics_answered(MEMBER_SUB, True)
    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_record_without_the_field_lets_the_member_submit(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    # Older Weave versions leave `demographics_answered` out; the gate logs a warning and lets it through.
    weave_stub.set_demographics_answered(MEMBER_SUB, None)
    project = create_project(login_as(MEMBER_SUB), title="T", write_up="W", budget_requested_cents=100)

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_weave_down_refuses_the_submission(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient, monkeypatch
) -> None:
    weave_stub.set_demographics_answered(MEMBER_SUB, True)
    project = create_project(login_as(MEMBER_SUB), title="T", write_up="W", budget_requested_cents=100)
    db_session.commit()
    csrf = get_csrf_token(client.get(f"/projects/{project.id}").text)
    real_get_user = weave_stub.get_user
    calls = 0

    def _down_on_the_gate(sub: str, *, fresh: bool = False):
        # The first fresh lookup is `fresh_actor`; the second is the gate.
        nonlocal calls
        if fresh:
            calls += 1
            if calls == 2:
                raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    monkeypatch.setattr(weave_stub, "get_user", _down_on_the_gate)
    response = client.post(
        f"/projects/{project.id}/submit", data={"csrf_token": csrf, "criteria_read": "yes"}, follow_redirects=False
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT
    monkeypatch.undo()
    assert "reach Weave to check your demographics answers" in client.get(response.headers["location"]).text


def test_saving_a_draft_is_not_gated(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_demographics_answered(MEMBER_SUB, False)
    project = create_project(login_as(MEMBER_SUB), title="", write_up="", budget_requested_cents=0)

    csrf = get_csrf_token(client.get(f"/projects/{project.id}/edit").text)
    response = client.post(
        f"/projects/{project.id}/edit",
        data={"csrf_token": csrf, "title": "T", "write_up": "W", "budget_requested": "10.00"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.title == "T"


def test_a_completion_is_not_gated(
    client: TestClient, login_as, approved_project, db_session: Session, weave_stub: StubWeaveClient
) -> None:
    reviewer = login_as(REVIEWER_SUB)
    member = login_as(MEMBER_SUB)
    project = approved_project(member, reviewer, title="T", write_up="W", budget_requested_cents=100)
    project_service.start_completion(db_session, actor_for(member), project=project)
    db_session.commit()
    weave_stub.set_demographics_answered(MEMBER_SUB, False)

    response = _post_submit(client, project.id, route="submit-completion")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_COMPLETION_REVIEW
