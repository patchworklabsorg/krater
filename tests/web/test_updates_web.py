"""Progress updates and top-up requests on the web and in the CLI's API (`krater.web.routers.updates`)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from krater.config import get_settings
from krater.models import ProjectUpdate, RevisionKind, RevisionOutcome
from krater.services import budget
from krater.slack import get_slack_client
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token


@pytest.fixture
def deferred(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "krater.web.routers.updates.slack_post_project_update.defer",
        lambda update_id: calls.append(("update", update_id)),
    )
    monkeypatch.setattr(
        "krater.web.routers.updates.slack_notify_revision_submitted.defer",
        lambda revision_id: calls.append(("revision", revision_id)),
    )
    monkeypatch.setattr(
        "krater.web.routers.api.slack_post_project_update.defer",
        lambda update_id: calls.append(("update", update_id)),
    )
    return calls


@pytest.fixture
def project(login_as, approved_project):
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    project = approved_project(member, reviewer, budget_requested_cents=5_000)
    login_as(MEMBER_SUB)
    return project


def _post(client, project, **data):
    csrf = get_csrf_token(client.get(f"/projects/{project.id}/updates/new").text)
    return client.post(f"/projects/{project.id}/updates", data={"csrf_token": csrf, **data}, follow_redirects=False)


def test_the_team_shares_progress(client, db_session, project, deferred) -> None:
    response = _post(client, project, body="Trained **the first model**.")

    assert response.status_code == 303
    update = db_session.scalars(sa.select(ProjectUpdate)).one()
    assert update.body == "Trained **the first model**."
    assert deferred == [("update", str(update.id))]
    page = client.get(f"/projects/{project.id}")
    assert "<strong>the first model</strong>" in page.text
    assert "Share progress" in page.text
    assert "Request a top-up" in page.text


def test_an_empty_update_is_refused(client, db_session, project, deferred) -> None:
    response = _post(client, project, body="  ")
    assert response.status_code == 422
    assert "Say what you" in response.text
    assert deferred == []


def test_the_team_requests_a_topup(client, db_session, project, deferred) -> None:
    form = client.get(f"/projects/{project.id}/updates/new?topup=1")
    assert "How much more (USD)" in form.text

    response = _post(
        client,
        project,
        topup="1",
        body="It trains.",
        amount="25",
        reason="Two more runs.",
        criteria_read="yes",
    )

    assert response.status_code == 303
    update = db_session.scalars(sa.select(ProjectUpdate)).one()
    db_session.refresh(project)
    revision = project.current_revision
    assert update.revision_id == revision.id
    assert revision.kind is RevisionKind.AMENDMENT
    assert revision.outcome is RevisionOutcome.PENDING
    assert revision.budget_requested_cents == 7_500
    assert set(deferred) == {("revision", str(revision.id)), ("update", str(update.id))}
    page = client.get(f"/projects/{project.id}")
    assert "Top-up request (in review)" in page.text
    assert "Two more runs." in page.text
    assert "Request a top-up" not in page.text  # one at a time


def test_a_topup_without_the_box_or_amount_is_refused(client, db_session, project, deferred) -> None:
    response = _post(client, project, topup="1", amount="lots", reason="More.")

    assert response.status_code == 422
    assert "Enter a valid dollar amount" in response.text
    assert "Submission Criteria, then tick the box" in response.text
    assert db_session.scalar(sa.select(sa.func.count()).select_from(ProjectUpdate)) == 0
    assert budget.ceiling_cents(db_session, project) == 5_000
    assert deferred == []


def test_reviewers_read_updates_but_cant_post(client, db_session, project, login_as, deferred) -> None:
    _post(client, project, body="Progress for reviewers.")
    login_as(REVIEWER_SUB)

    page = client.get(f"/projects/{project.id}")
    assert "Progress for reviewers." in page.text
    assert "Share progress" not in page.text
    assert client.get(f"/projects/{project.id}/updates/new").status_code == 404
    login_as(ADMIN_SUB)
    assert "Progress for reviewers." in client.get(f"/projects/{project.id}").text


def test_someone_off_the_team_sees_nothing(client, project, login_as) -> None:
    login_as(OTHER_MEMBER_SUB)
    assert client.get(f"/projects/{project.id}/updates/new").status_code == 404


def test_posting_needs_csrf(client, project) -> None:
    response = client.post(f"/projects/{project.id}/updates", data={"body": "Hi."}, follow_redirects=False)
    assert response.status_code in (400, 403)


def test_reviewers_and_outsiders_cant_post(client, db_session, project, login_as, deferred) -> None:
    csrf = get_csrf_token(client.get(f"/projects/{project.id}/updates/new").text)
    for sub in (REVIEWER_SUB, OTHER_MEMBER_SUB):
        login_as(sub)
        response = client.post(
            f"/projects/{project.id}/updates", data={"csrf_token": csrf, "body": "Not mine."}, follow_redirects=False
        )
        assert response.status_code in (
            403,
            404,
        )  # 404 for a page they may not open; 403 if the session's token changed
    assert db_session.scalar(sa.select(sa.func.count()).select_from(ProjectUpdate)) == 0


def test_a_slack_guest_shares_progress_but_cant_request_a_topup(client, db_session, project, deferred) -> None:
    get_slack_client().set_user_info("U0001MEMBER", is_restricted=True)
    try:
        refused = _post(client, project, topup="1", amount="25", reason="More.", criteria_read="yes")
        assert refused.status_code == 422
        assert db_session.scalar(sa.select(sa.func.count()).select_from(ProjectUpdate)) == 0
        db_session.refresh(project)
        assert project.current_revision.kind is RevisionKind.PROPOSAL  # no amendment was opened

        assert _post(client, project, body="Plain progress is fine.").status_code == 303
    finally:
        get_slack_client().unset_user_info("U0001MEMBER")


def test_unanswered_demographics_link_to_weave(client, db_session, project, weave_stub, deferred) -> None:
    weave_stub.set_demographics_answered(MEMBER_SUB, False)

    response = _post(client, project, topup="1", amount="25", reason="More.", criteria_read="yes")

    assert response.status_code == 422
    assert "answer the demographics form" in response.text.lower()
    assert db_session.scalar(sa.select(sa.func.count()).select_from(ProjectUpdate)) == 0


def test_no_full_amendment_while_a_topup_is_in_review(client, db_session, project, deferred) -> None:
    _post(client, project, topup="1", body="It trains.", amount="25", reason="More.", criteria_read="yes")

    page = client.get(f"/projects/{project.id}")
    assert "Start amendment" not in page.text
    response = client.post(
        f"/projects/{project.id}/amend", data={"csrf_token": get_csrf_token(page.text)}, follow_redirects=False
    )
    assert response.status_code == 303
    db_session.refresh(project)
    assert project.current_revision.topup is not None  # still the top-up, not superseded


# -- The CLI's API ----------------------------------------------------------------------------------------------------


@pytest.fixture
def api_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")


def _as(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {StubWeaveClient.cli_token_for(sub)}"}


def test_the_api_posts_and_lists_updates(client, project, api_on, deferred) -> None:
    posted = client.post(
        f"/api/v1/projects/{project.id}/updates", json={"body": "From the CLI."}, headers=_as(MEMBER_SUB)
    )
    assert posted.status_code == 201, posted.text
    assert deferred == [("update", posted.json()["id"])]

    listed = client.get(f"/api/v1/projects/{project.id}/updates", headers=_as(REVIEWER_SUB)).json()
    [update] = listed["updates"]
    assert update["body"] == "From the CLI."
    assert (update["ceiling_cents"], update["topup_cents"]) == (5_000, None)
    assert not listed["can_post"]
    assert listed["topup_url"].endswith(f"/projects/{project.id}/updates/new?topup=1")


def test_the_api_refuses_outsiders_and_reviewers(client, project, api_on, deferred, login_as) -> None:
    login_as(OTHER_MEMBER_SUB)  # the API knows only people who signed in to Krater once
    outsider = client.post(
        f"/api/v1/projects/{project.id}/updates", json={"body": "Hi."}, headers=_as(OTHER_MEMBER_SUB)
    )
    assert outsider.status_code == 404
    reviewer = client.post(f"/api/v1/projects/{project.id}/updates", json={"body": "Hi."}, headers=_as(REVIEWER_SUB))
    assert reviewer.status_code == 403
    empty = client.post(f"/api/v1/projects/{project.id}/updates", json={"body": " "}, headers=_as(MEMBER_SUB))
    assert empty.status_code == 422
    assert deferred == []
