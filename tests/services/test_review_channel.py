"""The reviewers-only Slack channel (`ganymede-rev-...`): the review message and its buttons go there, the team's own
channel gets a notice the decision updates, and nobody on the team is ever in it."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import ProjectStatus, ReviewDecision, ReviewSource
from krater.services import projects, slack_notify
from krater.services.actor import Actor
from krater.slack.fake import FakeSlackClient
from krater.weave import StubWeaveClient
from tests.factories import new_project
from tests.services.conftest import add_to_team


def _submitted(db_session: Session, member: Actor, reviewer: Actor):
    member.user.slack_user_id = "U_MEMBER"
    reviewer.user.slack_user_id = "U_REVIEWER"
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=5000)
    return projects.submit(db_session, member, project=project, criteria_read=True)


def _notify(db_session, slack_client, weave, project):
    revision = project.current_revision
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)
    return revision


def test_the_review_message_goes_to_a_reviewers_only_channel(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()

    revision = _notify(db_session, slack_client, weave, project)

    review_channel = slack_client.channels[project.slack_review_channel_id]
    assert review_channel["name"] == slack_notify.review_channel_name_for(project)
    assert review_channel["name"].startswith("ganymede-rev-rover-")
    assert review_channel["members"] == {"U_REVIEWER"}
    assert revision.slack_message_channel_id == project.slack_review_channel_id
    message = slack_client.messages[(project.slack_review_channel_id, revision.slack_message_ts)]
    assert message["blocks"][-1]["block_id"] == "review_actions"  # the buttons stay last
    assert f"<#{project.slack_channel_id}>" in str(message["blocks"])

    # The team's channel has the team and reviewers, and a notice with no buttons.
    assert slack_client.channels[project.slack_channel_id]["members"] == {"U_MEMBER", "U_REVIEWER"}
    notice = slack_client.messages[(project.slack_channel_id, revision.slack_team_message_ts)]
    assert "submitted" in notice["text"]
    assert all(block["type"] != "actions" for block in notice["blocks"])


def test_a_resubmission_reuses_the_reviewer_channel(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    _notify(db_session, slack_client, weave, project)
    first_channel = project.slack_review_channel_id
    projects.admin_decide(
        db_session,
        admin,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="Say more about the rover.",
        weave_client=weave,
    )
    project = projects.submit(db_session, member, project=project, criteria_read=True)

    _notify(db_session, slack_client, weave, project)

    assert project.slack_review_channel_id == first_channel
    assert len(slack_client.channels) == 2


def test_the_decision_updates_both_messages(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    revision = _notify(db_session, slack_client, weave, project)

    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.REJECT,
        reason="Tell us how you'll test it.",
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    review = slack_client.messages[(project.slack_review_channel_id, revision.slack_message_ts)]
    notice = slack_client.messages[(project.slack_channel_id, revision.slack_team_message_ts)]
    for message in (review, notice):
        assert all(block["type"] != "actions" for block in message["blocks"])
        assert "Tell us how you'll test it." in str(message["blocks"])


def test_a_review_message_from_before_keeps_working_in_the_team_channel(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    """A revision whose message was posted in the team channel (no team notice) is updated there, and nothing is
    posted twice."""
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    revision = project.current_revision
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-old")
    revision.slack_message_channel_id = project.slack_channel_id
    revision.slack_message_ts = slack_client.post_message(project.slack_channel_id, blocks=[], text="old")
    db_session.flush()

    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        reason=None,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    assert project.slack_review_channel_id is None
    assert len(slack_client.messages) == 1
    assert "Outcome" in str(slack_client.messages[(project.slack_channel_id, revision.slack_message_ts)]["blocks"])


def test_a_reviewer_who_joins_the_team_leaves_the_reviewer_channel(
    db_session: Session, member: Actor, reviewer: Actor, make_actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    _notify(db_session, slack_client, weave, project)
    assert "U_REVIEWER" in slack_client.channels[project.slack_review_channel_id]["members"]

    add_to_team(db_session, project, reviewer, accepted=False)  # just invited is enough
    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_REVIEWER" not in slack_client.channels[project.slack_review_channel_id]["members"]
    assert "U_REVIEWER" in slack_client.channels[project.slack_channel_id]["members"]

    # sync_review_channel (run when someone accepts) does the same.
    slack_client.invite_users(project.slack_review_channel_id, ["U_REVIEWER"])
    slack_notify.sync_review_channel(db_session, slack_client, weave, project=project)
    assert "U_REVIEWER" not in slack_client.channels[project.slack_review_channel_id]["members"]


def test_a_new_reviewer_is_invited_to_open_reviewer_channels(
    db_session: Session, member: Actor, reviewer: Actor, make_actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    _notify(db_session, slack_client, weave, project)
    newcomer = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}), display_name="New Reviewer")
    newcomer.user.slack_user_id = "U_NEW_REVIEWER"
    db_session.flush()

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_NEW_REVIEWER" in slack_client.channels[project.slack_review_channel_id]["members"]
    assert "U_MEMBER" not in slack_client.channels[project.slack_review_channel_id]["members"]


def test_the_reconcile_creates_no_reviewer_channel_for_an_older_project(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    """Reviewer channels come with a review message, never from the periodic sync."""
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-old")
    db_session.flush()

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)
    slack_notify.sync_missed_archives(db_session, slack_client)

    assert project.slack_review_channel_id is None
    assert len(slack_client.channels) == 1


def test_finishing_the_project_archives_both_channels(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    _notify(db_session, slack_client, weave, project)
    project.status = ProjectStatus.WITHDRAWN
    db_session.flush()

    slack_notify.archive_project_channel(db_session, slack_client, project=project)

    assert slack_client.channels[project.slack_channel_id]["archived"]
    assert slack_client.channels[project.slack_review_channel_id]["archived"]
    assert project.slack_review_channel_archived


def test_the_reconcile_archives_a_missed_reviewer_channel(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, reviewer)
    slack_client = FakeSlackClient()
    _notify(db_session, slack_client, weave, project)
    project.status = ProjectStatus.WITHDRAWN
    project.slack_channel_archived = True  # the team channel was archived; the reviewer channel was missed
    db_session.flush()

    slack_notify.sync_missed_archives(db_session, slack_client)

    assert slack_client.channels[project.slack_review_channel_id]["archived"]
