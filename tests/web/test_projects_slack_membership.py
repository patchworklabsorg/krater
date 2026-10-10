"""The Slack membership gate (`krater.services.slack_membership`), enforced by the submit routes before
handing off to `project_service` -- see `docs/SPEC.md` "Roles & authentication". Weave's `slack_member`
decides when Weave gives one; otherwise Slack decides: the user's Slack account (stored id, else a
lookup by verified email) must exist and not be deleted or a guest. Drafts are always allowed; only
submitting is gated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import ProjectStatus
from krater.services import projects as project_service
from krater.slack import get_slack_client
from krater.weave import StubWeaveClient
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import actor_for

# `MEMBER_SUB` (PWLMEMBERONE, stub_users.json) carries `slack_id: "U0001MEMBER"`, stored at sign-in.
MEMBER_SLACK_ID = "U0001MEMBER"
# PWLSLACKGUEST in stub_users.json: a Ganymede member Weave reports with `slack_member: false` (and whom the
# fake Slack also reports as a guest).
GUEST_SUB = "PWLSLACKGUEST"


@pytest.fixture
def slack_user_state() -> Iterator[Callable[..., None]]:
    """Override what the fake Slack says about `MEMBER_SLACK_ID`, restored afterwards."""
    slack_client = get_slack_client()

    def _set(**state: bool) -> None:
        slack_client.set_user_info(MEMBER_SLACK_ID, **state)

    yield _set
    slack_client.unset_user_info(MEMBER_SLACK_ID)


def _post_submit(client: TestClient, project_id, route: str = "submit"):
    csrf = get_csrf_token(client.get(f"/projects/{project_id}").text)
    return client.post(f"/projects/{project_id}/{route}", data={"csrf_token": csrf}, follow_redirects=False)


def test_a_slack_guest_cannot_submit(client: TestClient, login_as, create_project, db_session: Session) -> None:
    guest = login_as(GUEST_SUB)
    project = create_project(guest, title="T", write_up="W", budget_requested_cents=100)
    # Mirrors production: the draft is created (and committed) in an earlier request.
    db_session.commit()

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT  # never made it to project_service.submit

    redirected = client.get(response.headers["location"])
    assert "code of conduct" in redirected.text.lower()


def test_a_full_member_can_submit(client: TestClient, login_as, create_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="T", write_up="W", budget_requested_cents=100)

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


@pytest.mark.parametrize(
    "slack_state",
    [{"is_restricted": True}, {"is_ultra_restricted": True}, {"deleted": True}],
    ids=["restricted", "ultra-restricted", "deleted"],
)
def test_slack_guests_and_deactivated_accounts_cannot_submit(
    client: TestClient, login_as, create_project, db_session: Session, slack_user_state, slack_state: dict
) -> None:
    slack_user_state(**slack_state)
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="T", write_up="W", budget_requested_cents=100)
    db_session.commit()

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT


def test_a_member_with_no_slack_account_cannot_submit(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    # PWLMEMBERTWO has no `slack_id` in the fixture, and the fake Slack knows no account for their email.
    member = login_as(OTHER_MEMBER_SUB)
    project = create_project(member, title="T", write_up="W", budget_requested_cents=100)
    db_session.commit()

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT


def test_a_member_found_by_email_can_submit_and_gets_linked(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    member = login_as(OTHER_MEMBER_SUB)
    slack_client = get_slack_client()
    slack_client.register_email(member.email, "U0009BYMAIL")
    try:
        project = create_project(member, title="T", write_up="W", budget_requested_cents=100)

        response = _post_submit(client, project.id)
    finally:
        slack_client.unregister_email(member.email)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW
    assert member.slack_user_id == "U0009BYMAIL"


def test_a_slack_guest_cannot_submit_a_completion(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    reviewer = login_as(REVIEWER_SUB)
    guest = login_as(GUEST_SUB)
    project = approved_project(guest, reviewer, title="T", write_up="W", budget_requested_cents=100)
    project_service.start_completion(db_session, actor_for(guest), project=project)
    db_session.commit()
    status_before = project.status

    response = _post_submit(client, project.id, route="submit-completion")

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is status_before
    assert project.status is not ProjectStatus.PENDING_COMPLETION_REVIEW


def test_drafts_are_always_allowed_even_for_a_slack_guest(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    guest = login_as(GUEST_SUB)
    project = create_project(guest, title="", write_up="", budget_requested_cents=0)

    page = client.get(f"/projects/{project.id}/edit")
    csrf = get_csrf_token(page.text)
    response = client.post(
        f"/projects/{project.id}/edit",
        data={"csrf_token": csrf, "title": "T", "write_up": "W", "budget_requested": "10.00"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT
    assert project.title == "T"


def test_weave_saying_slack_member_true_lets_a_slack_guest_submit(
    client: TestClient, login_as, create_project, db_session: Session, weave_stub: StubWeaveClient, slack_user_state
) -> None:
    # Weave's answer is preferred: the fake Slack calls this account a guest, Weave says full member.
    member = login_as(MEMBER_SUB)
    slack_user_state(is_ultra_restricted=True)
    record = weave_stub.get_user(MEMBER_SUB)
    assert record is not None
    weave_stub.put_user(
        MEMBER_SUB, name=record.name, email=record.email, slack_id=record.slack_id, slack_member=True, roles=["member"]
    )
    project = create_project(member, title="T", write_up="W", budget_requested_cents=100)

    response = _post_submit(client, project.id)

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW
