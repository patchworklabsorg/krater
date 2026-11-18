"""Progress updates and top-up requests in Slack (`slack_notify.post_project_update`, the top-up review message)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from krater.models import Project, ReviewDecision, ReviewSource
from krater.services import project_updates, projects, slack_notify
from krater.services.actor import Actor
from krater.slack.errors import SlackRequestFailedError
from krater.slack.fake import FakeSlackClient
from krater.weave import StubWeaveClient
from tests.factories import new_project


def _approved_with_channel(db_session: Session, member: Actor, reviewer: Actor, weave) -> Project:
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=5_000)
    projects.submit(db_session, member, project=project, criteria_read=True)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(project)
    project.slack_channel_id = "C_TEAM"
    db_session.flush()
    return project


def _text(message: dict) -> str:
    return json.dumps(message["blocks"])


def test_an_update_is_posted_once_to_the_team_channel(db_session, member, reviewer, weave) -> None:
    project = _approved_with_channel(db_session, member, reviewer, weave)
    member.user.slack_user_id = "U_MEMBER"
    update = project_updates.post(db_session, member, project=project, body="Trained *it* <@here>.")
    slack = FakeSlackClient()

    slack_notify.post_project_update(db_session, slack, update_id=update.id)
    slack_notify.post_project_update(db_session, slack, update_id=update.id)

    [(channel, ts)] = list(slack.messages)
    assert channel == "C_TEAM"
    assert update.slack_message_ts == ts and update.slack_posted_at is not None
    text = _text(slack.messages[(channel, ts)])
    assert "Progress update" in text and "<@U_MEMBER>" in text
    assert "<@here>" not in text  # escaped, so it doesn't ping the channel
    assert "Spent $0.00 of $50.00 (0%)" in text


def test_an_archived_channel_marks_it_done_unposted(db_session, member, reviewer, weave) -> None:
    project = _approved_with_channel(db_session, member, reviewer, weave)
    project.slack_channel_archived = True
    update = project_updates.post(db_session, member, project=project, body="Late.")
    slack = FakeSlackClient()

    slack_notify.post_project_update(db_session, slack, update_id=update.id)

    assert slack.messages == {}
    assert update.slack_posted_at is not None


def test_the_reconcile_posts_an_update_its_job_missed(db_session, member, reviewer, weave) -> None:
    project = _approved_with_channel(db_session, member, reviewer, weave)
    update = project_updates.post(db_session, member, project=project, body="Missed.")
    update.created_at = datetime.now(UTC) - timedelta(minutes=30)
    db_session.flush()
    slack = FakeSlackClient()

    slack_notify.sync_project_updates(db_session, slack)

    assert update.slack_posted_at is not None
    assert len(slack.messages) == 1


def test_the_reconcile_leaves_new_and_old_updates(db_session, member, reviewer, weave) -> None:
    project = _approved_with_channel(db_session, member, reviewer, weave)
    fresh = project_updates.post(db_session, member, project=project, body="Just now.")
    old = project_updates.post(db_session, member, project=project, body="Long ago.")
    old.created_at = datetime.now(UTC) - slack_notify.PROJECT_UPDATE_WINDOW - timedelta(hours=1)
    db_session.flush()
    slack = FakeSlackClient()

    slack_notify.sync_project_updates(db_session, slack)

    assert slack.messages == {}
    assert fresh.slack_posted_at is None and old.slack_posted_at is None


def test_a_slack_refusal_is_retried_later(db_session, member, reviewer, weave, monkeypatch) -> None:
    project = _approved_with_channel(db_session, member, reviewer, weave)
    update = project_updates.post(db_session, member, project=project, body="Hello.")
    slack = FakeSlackClient()

    def refuse(*args, **kwargs):
        raise SlackRequestFailedError("channel_not_found")

    monkeypatch.setattr(slack, "post_message", refuse)
    with pytest.raises(SlackRequestFailedError):
        slack_notify.post_project_update(db_session, slack, update_id=update.id)
    assert update.slack_posted_at is None


def test_a_topup_review_message_shows_the_ask(db_session, member, reviewer, weave: StubWeaveClient) -> None:
    project = _approved_with_channel(db_session, member, reviewer, weave)
    update = project_updates.request_topup(
        db_session,
        member,
        project=project,
        amount_cents=2_500,
        reason="Two more runs.",
        body="It trains.",
        criteria_read=True,
    )
    slack = FakeSlackClient()

    slack_notify.notify_revision_submitted(db_session, slack, weave, revision=update.revision, feed_channel_id=None)

    review = slack.messages[(update.revision.slack_message_channel_id, update.revision.slack_message_ts)]
    text = _text(review)
    assert "Top-up request: Rover" in text
    assert "$25.00 more" in text and "$75.00" in text
    assert "Two more runs." in text and "It trains." in text
    assert review["blocks"][-1]["elements"][0]["value"] == str(update.revision.id)
