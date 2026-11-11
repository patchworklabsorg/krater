"""Channel/message upkeep (`krater.services.slack_notify`), against `FakeSlackClient` -- creation,
invites, idempotency, decision updates, admin overrides, archiving and the periodic reconcile steps.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from krater.models import AuditEvent, FundingHold, ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import grants, projects, slack_notify
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.slack.errors import SlackRequestFailedError
from krater.slack.fake import FakeSlackClient
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.factories import new_project
from tests.services.conftest import add_to_team


def test_ensure_channel_creates_and_invites_the_team(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    member.user.slack_user_id = "U_MEMBER"
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)

    assert project.slack_channel_id == channel_id
    assert slack_client.channels[channel_id]["name"] == slack_notify.channel_name_for(project)
    assert slack_client.channels[channel_id]["members"] == {"U_MEMBER", "U_REVIEWER"}


def test_ensure_channel_is_idempotent(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    first = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)
    second = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)

    assert first == second
    assert len(slack_client.channels) == 1


def test_notify_revision_submitted_posts_a_review_message_and_a_feed_line(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    assert revision.slack_message_ts is not None
    channel_id = revision.slack_message_channel_id
    assert (channel_id, revision.slack_message_ts) in slack_client.messages
    review_actions = slack_client.messages[(channel_id, revision.slack_message_ts)]["blocks"][-1]
    assert review_actions["elements"][0]["value"] == str(revision.id)

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert len(feed_messages) == 1
    assert "Rover" in feed_messages[0]["text"]


def test_notify_revision_submitted_is_idempotent(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    assert len(slack_client.channels) == 1
    assert len(slack_client.messages) == 2  # the review message + the one feed line, not doubled


def test_no_feed_line_for_a_resubmission_after_rejection(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="no",
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(project)
    projects.update_draft(db_session, member, project=project, write_up="revised")
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    assert revision.number == 2

    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert feed_messages == []


def test_no_feed_line_for_an_amendment(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(project)
    projects.start_amendment(db_session, member, project=project)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision

    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert feed_messages == []


def test_notify_decision_updates_the_message_and_drops_the_buttons(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)

    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.APPROVED

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert not any(block.get("type") == "actions" for block in updated["blocks"])
    assert "Approved" in updated["text"]


def test_notify_decision_says_when_less_was_granted_and_why(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=18_000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)
    posted = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert "To grant less, approve on" in str(posted["blocks"])

    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        reason="A smaller first step.",
        source=ReviewSource.WEB,
        weave_client=weave,
        budget_cents=5_000,
    )
    db_session.refresh(revision)
    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert "Approved for $50.00 of the $180.00 asked for." in updated["text"]
    assert "*Reason:* A smaller first step." in str(updated["blocks"])


def test_notify_decision_says_an_approval_is_awaiting_funding(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)
    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
        funding_hold=FundingHold.AWAITING_FUNDING,
    )
    db_session.refresh(revision)

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert "Approved · awaiting funding" in updated["text"]


def test_notify_decision_is_a_no_op_while_still_pending(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)

    before = dict(slack_client.messages)
    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    assert slack_client.messages == before


def test_post_admin_override_posts_to_the_channel(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")

    slack_notify.post_admin_override(
        db_session, slack_client, project=project, action="admin_approve", actor_name="Ana Admin", reason="urgent"
    )

    ((channel_id, _ts), message) = next(iter(slack_client.messages.items()))
    assert channel_id == project.slack_channel_id
    assert "admin_approve" in message["text"]
    assert "Ana Admin" in message["text"]
    assert "urgent" in message["text"]


def test_post_admin_override_is_a_no_op_without_a_channel(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()

    slack_notify.post_admin_override(
        db_session, slack_client, project=project, action="admin_withdraw", actor_name="Ana Admin", reason=None
    )

    assert slack_client.messages == {}


def test_archive_project_channel_is_idempotent(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.WITHDRAWN

    slack_notify.archive_project_channel(db_session, slack_client, project=project)
    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True

    # A second call must not re-archive (harmless either way, but proves the guard works).
    slack_client.channels[project.slack_channel_id]["archived"] = False
    slack_notify.archive_project_channel(db_session, slack_client, project=project)
    assert slack_client.channels[project.slack_channel_id]["archived"] is False


def test_archive_project_channel_is_a_no_op_while_not_terminal(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")

    slack_notify.archive_project_channel(db_session, slack_client, project=project)

    assert project.slack_channel_archived is False
    assert slack_client.channels[project.slack_channel_id]["archived"] is False


def test_sync_reviewer_invites_invites_reviewers_to_open_channels(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    reviewer.user.slack_user_id = "U_NEW_REVIEWER"

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_NEW_REVIEWER" in slack_client.channels[project.slack_channel_id]["members"]


def test_sync_reviewer_invites_skips_archived_channels(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.slack_channel_archived = True
    reviewer.user.slack_user_id = "U_NEW_REVIEWER"

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_NEW_REVIEWER" not in slack_client.channels[project.slack_channel_id]["members"]


def _team_invites(
    db_session: Session, weave: StubWeaveClient, project, slack_client: FakeSlackClient | None = None
) -> set[str]:
    slack_client = slack_client or FakeSlackClient()
    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)
    return slack_client.channels[channel_id]["members"]


def test_ensure_channel_invites_reviewers_weave_lists_and_leaves_out_inactive_ones(
    db_session: Session, member: Actor, reviewer: Actor, make_actor, weave: StubWeaveClient
) -> None:
    locked_reviewer = make_actor(groups=reviewer.groups, slack_user_id="U_LOCKED")
    weave.set_active(locked_reviewer.user.weave_sub, False)
    plain_member = make_actor(groups=frozenset({GROUP_MEMBER}), slack_user_id="U_PLAIN")
    member.user.slack_user_id = "U_MEMBER"
    reviewer.user.slack_user_id = "U_REVIEWER"
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)

    invited = _team_invites(db_session, weave, project)

    assert invited == {"U_MEMBER", "U_REVIEWER"}
    assert plain_member.user.slack_user_id not in invited


def test_ensure_channel_uses_the_slack_id_weave_reports_for_a_reviewer_krater_has_never_seen(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    weave.put_user("PWLNEVERSEEN", name="Nev", email="nev@example.com", roles=["member", "reviewer"], slack_id="U_NEV")
    member.user.slack_user_id = "U_MEMBER"
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)

    assert _team_invites(db_session, weave, project) == {"U_MEMBER", "U_NEV"}


def test_ensure_channel_invites_slack_guests_too_and_leaves_refusals_to_slack(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    # Krater no longer filters by guest status: the live client invites with `force` and skips anyone
    # Slack refuses, so a guest can't stop the rest of the team's invites.
    make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}), slack_user_id="U_GUEST")
    member.user.slack_user_id = "U_MEMBER"
    slack_client = FakeSlackClient()
    slack_client.set_user_info("U_GUEST", is_ultra_restricted=True)
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)

    assert _team_invites(db_session, weave, project, slack_client) == {"U_MEMBER", "U_GUEST"}


def test_ensure_channel_invites_the_team_but_not_pending_invitees(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    teammate = make_actor(slack_user_id="U_TEAMMATE")
    unlinked_teammate = make_actor()
    invitee = make_actor(slack_user_id="U_INVITEE")
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    add_to_team(db_session, project, teammate)
    add_to_team(db_session, project, unlinked_teammate)
    add_to_team(db_session, project, invitee, accepted=False)
    member.user.slack_user_id = "U_MEMBER"

    assert _team_invites(db_session, weave, project) == {"U_MEMBER", "U_TEAMMATE"}


def test_ensure_channel_leaves_out_people_without_a_slack_id_from_weave(
    db_session: Session, member: Actor, reviewer: Actor, make_actor, weave: StubWeaveClient
) -> None:
    # No Slack lookups by email: Krater keeps no emails (patchworklabsorg/krater#7).
    make_actor(groups=reviewer.groups, slack_user_id="U_LINKED")
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    slack_client = FakeSlackClient()

    assert _team_invites(db_session, weave, project, slack_client) == {"U_LINKED"}


def test_sync_reviewer_invites_sees_a_reviewer_role_weave_granted_after_the_channel_exists(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    newcomer = make_actor(groups=frozenset({GROUP_MEMBER}), slack_user_id="U_NEWCOMER")

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)
    assert "U_NEWCOMER" not in slack_client.channels[project.slack_channel_id]["members"]

    weave.set_roles(newcomer.user.weave_sub, ["member", "reviewer"])
    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_NEWCOMER" in slack_client.channels[project.slack_channel_id]["members"]


class _OneBrokenChannelSlackClient(FakeSlackClient):
    def __init__(self) -> None:
        super().__init__()
        self.broken_channel_id: str | None = None

    def invite_users(self, channel_id: str, slack_user_ids: list[str]) -> None:
        if channel_id == self.broken_channel_id:
            raise SlackRequestFailedError("conversations.invite failed: is_archived")
        super().invite_users(channel_id, slack_user_ids)


def test_sync_reviewer_invites_carries_on_past_a_channel_slack_refuses(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    broken = new_project(db_session, member, title="Broken", write_up="w", budget_requested_cents=5000)
    fine = new_project(db_session, member, title="Fine", write_up="w", budget_requested_cents=5000)
    slack_client = _OneBrokenChannelSlackClient()
    broken.slack_channel_id = slack_client.create_channel("ganymede-broken")
    fine.slack_channel_id = slack_client.create_channel("ganymede-fine")
    slack_client.broken_channel_id = broken.slack_channel_id
    db_session.flush()
    reviewer.user.slack_user_id = "U_REVIEWER"

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_REVIEWER" in slack_client.channels[fine.slack_channel_id]["members"]


def test_sync_budget_notifications_posts_a_warning_once(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    event = AuditEvent(
        actor_id=None,
        action=slack_notify.AUDIT_BUDGET_WARNING,
        project_id=project.id,
        payload={"ceiling_cents": 5000, "spend_cents": 4000, "percent": 80.0},
    )
    db_session.add(event)
    db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)
    assert len(slack_client.messages) == 1

    # A second reconcile pass must not post it again.
    slack_notify.sync_budget_notifications(db_session, slack_client)
    assert len(slack_client.messages) == 1


def test_a_held_and_a_released_grant_are_posted_to_the_channel(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    for action in (grants.AUDIT_GRANT_HELD, grants.AUDIT_GRANT_RELEASED):
        db_session.add(AuditEvent(actor_id=None, action=action, project_id=project.id, payload={"amount_cents": 5000}))
        db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)

    texts = sorted(message["text"] for message in slack_client.messages.values())
    assert len(texts) == 2
    held = next(text for text in texts if "Budget held" in text)
    released = next(text for text in texts if "Budget released" in text)
    assert "$50.00" in held and f"/projects/{project.id}" in held
    assert "$50.00" in released


def test_a_grant_held_by_a_funding_hold_doesnt_blame_verification(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    db_session.add(
        AuditEvent(
            actor_id=None,
            action=grants.AUDIT_GRANT_HELD,
            project_id=project.id,
            payload={"amount_cents": 5000, "funding_hold": "awaiting_third_party_review"},
        )
    )
    db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)

    (message,) = slack_client.messages.values()
    assert "Budget held" in message["text"] and "$50.00" in message["text"]
    assert "awaiting 3rd party review" in message["text"]
    assert "once an admin clears the hold" in message["text"]
    assert "verified" not in message["text"]


def test_a_held_grant_dms_each_member_it_waits_on(db_session: Session, member: Actor, make_actor) -> None:
    waiting = make_actor(slack_user_id="U_WAITING")
    make_actor(slack_user_id="U_CLEARED")  # Weave cleared them, so not in `waiting_on`
    no_slack = make_actor()
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    db_session.add(
        AuditEvent(
            actor_id=None,
            action=grants.AUDIT_GRANT_HELD,
            project_id=project.id,
            payload={"amount_cents": 5000, "waiting_on": [waiting.user.weave_sub, no_slack.user.weave_sub]},
        )
    )
    db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)

    by_channel = {channel: message["text"] for (channel, _ts), message in slack_client.messages.items()}
    assert set(by_channel) == {project.slack_channel_id, "U_WAITING"}
    assert "U_CLEARED" not in by_channel
    dm = by_channel["U_WAITING"]
    assert "budget is held until every team member is verified in Weave, you included" in dm
    assert f"/projects/{project.id}" in dm

    # The next reconcile pass posts neither again.
    slack_notify.sync_budget_notifications(db_session, slack_client)
    assert len(slack_client.messages) == 2


def test_the_final_budget_warning_tells_members_to_save_their_work(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    db_session.add(
        AuditEvent(
            actor_id=None,
            action=slack_notify.AUDIT_BUDGET_WARNING,
            project_id=project.id,
            payload={"ceiling_cents": 5000, "spend_cents": 4800, "percent": 96.0, "threshold": 95, "final": True},
        )
    )
    db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)

    (message,) = slack_client.messages.values()
    assert "Final budget warning" in message["text"] and "Save your work now" in message["text"]


class _BrokenChannelSlack(FakeSlackClient):
    def __init__(self) -> None:
        super().__init__()
        self.broken_channel_id: str | None = None

    def post_message(self, channel_id: str, *, blocks: list[dict], text: str) -> str:
        if channel_id == self.broken_channel_id:
            raise SlackRequestFailedError("chat.postMessage failed: not_in_channel")
        return super().post_message(channel_id, blocks=blocks, text=text)


def test_budget_notices_skip_archived_channels_and_survive_a_refusal(db_session: Session, member: Actor) -> None:
    """Seen on staging: a completed project's notice went to its archived channel, Slack refused it (`is_archived`)
    on every reconcile, and the loop stopped there, holding up every other project's notices."""
    slack_client = _BrokenChannelSlack()
    archived = new_project(db_session, member, title="Done", write_up="w", budget_requested_cents=5000)
    archived.slack_channel_id = slack_client.create_channel("ganymede-done")
    archived.slack_channel_archived = True
    broken = new_project(db_session, member, title="Kicked", write_up="w", budget_requested_cents=5000)
    broken.slack_channel_id = slack_client.create_channel("ganymede-kicked")
    slack_client.broken_channel_id = broken.slack_channel_id
    fine = new_project(db_session, member, title="Fine", write_up="w", budget_requested_cents=5000)
    fine.slack_channel_id = slack_client.create_channel("ganymede-fine")
    for project in (archived, broken, fine):
        db_session.add(
            AuditEvent(
                actor_id=None,
                action=slack_notify.AUDIT_BUDGET_WARNING,
                project_id=project.id,
                payload={"ceiling_cents": 5000, "spend_cents": 4000, "percent": 80.0},
            )
        )
    db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)

    assert [channel for channel, _ts in slack_client.messages] == [fine.slack_channel_id]
    slack_notify.sync_budget_notifications(db_session, slack_client)  # the archived one is handled, not retried
    assert len(slack_client.messages) == 1


def test_sync_missed_archives_archives_finished_projects(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED

    slack_notify.sync_missed_archives(db_session, slack_client)

    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True


def test_reconcile_runs_every_step(db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED
    db_session.flush()
    db_session.commit()

    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_notify.reconcile(db_session, slack_client, weave)

    assert slack_client.channels[project.slack_channel_id]["archived"] is True


def test_reconcile_skips_a_step_weave_cant_answer_and_runs_the_rest(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient, monkeypatch
) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED
    db_session.flush()
    db_session.commit()

    def weave_down(role):
        raise WeaveUnavailableError("Weave's directory returned 429")

    monkeypatch.setattr(weave, "list_users_with_role", weave_down)
    failed = slack_notify.reconcile(db_session, slack_client, weave)

    assert failed == ["sync_reviewer_invites"]
    assert slack_client.channels[project.slack_channel_id]["archived"] is True  # the later steps still ran


def _submitted_minutes_ago(db_session: Session, member: Actor, minutes: int, title: str = "Rover"):
    project = new_project(db_session, member, title=title, write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    revision.submitted_at = revision.submitted_at - timedelta(minutes=minutes)
    db_session.flush()
    return revision


def test_sync_missed_review_messages_posts_a_message_the_submission_job_missed(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    revision = _submitted_minutes_ago(db_session, member, 30)
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id="C_FEED")

    assert revision.slack_message_ts is not None
    assert (revision.slack_message_channel_id, revision.slack_message_ts) in slack_client.messages
    assert slack_client.channels[revision.slack_message_channel_id]["members"] >= {"U_REVIEWER"}
    assert [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]


def test_sync_missed_review_messages_leaves_a_fresh_submission_to_its_own_job(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    revision = _submitted_minutes_ago(db_session, member, 1)
    slack_client = FakeSlackClient()

    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id=None)

    assert revision.slack_message_ts is None
    assert slack_client.messages == {}


def test_sync_missed_review_messages_doesnt_post_twice(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    _submitted_minutes_ago(db_session, member, 30)
    slack_client = FakeSlackClient()

    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id=None)
    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id=None)

    assert len(slack_client.messages) == 1


def test_sync_missed_review_messages_skips_decided_revisions(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    revision = _submitted_minutes_ago(db_session, member, 30)
    revision.outcome = RevisionOutcome.REJECTED
    db_session.flush()
    slack_client = FakeSlackClient()

    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id=None)

    assert slack_client.messages == {}


def test_sync_missed_review_messages_leaves_out_submissions_older_than_the_window(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    revision = _submitted_minutes_ago(db_session, member, 8 * 24 * 60)
    slack_client = FakeSlackClient()

    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id=None)

    assert revision.slack_message_ts is None
    assert slack_client.messages == {}


def test_sync_missed_review_messages_carries_on_past_a_revision_slack_refuses(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    slack_client = _BrokenChannelSlack()
    broken = _submitted_minutes_ago(db_session, member, 60, title="Kicked")
    broken.project.slack_channel_id = slack_client.create_channel("ganymede-kicked")
    slack_client.broken_channel_id = broken.project.slack_channel_id
    fine = _submitted_minutes_ago(db_session, member, 30, title="Fine")
    db_session.commit()

    with pytest.raises(SlackRequestFailedError):
        slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id=None)

    assert broken.slack_message_ts is None
    assert fine.slack_message_ts is not None


def test_a_failed_feed_line_keeps_the_review_message_and_doesnt_post_it_twice(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    slack_client = _BrokenChannelSlack()
    slack_client.broken_channel_id = "C_FEED"
    revision = _submitted_minutes_ago(db_session, member, 30)
    db_session.commit()

    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id="C_FEED")
    slack_notify.sync_missed_review_messages(db_session, slack_client, weave, feed_channel_id="C_FEED")

    assert revision.slack_message_ts is not None
    assert len(slack_client.messages) == 1


def test_reconcile_posts_a_missed_review_message(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    revision = _submitted_minutes_ago(db_session, member, 30)
    db_session.commit()
    slack_client = FakeSlackClient()

    assert slack_notify.reconcile(db_session, slack_client, weave, feed_channel_id="C_FEED") == []

    assert revision.slack_message_ts is not None


def test_reconcile_reports_no_failed_steps_on_a_good_run(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    assert slack_notify.reconcile(db_session, FakeSlackClient(), weave) == []


# --------------------------------------------------------------------------------------------------
# mrkdwn injection: a project title, write-up, reject reason or admin display name containing Slack
# mrkdwn special characters must render as literal text, not be interpreted (`<!channel>`, a link
# hijack, etc.).
# --------------------------------------------------------------------------------------------------

_INJECTION = "<!channel> ignore this <https://phish.example/|Approve>"
_ESCAPED_INJECTION = "&lt;!channel&gt; ignore this &lt;https://phish.example/|Approve&gt;"


def test_review_message_escapes_the_project_title_and_write_up(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(
        db_session, member, title=_INJECTION, summary=_INJECTION, write_up=_INJECTION, budget_requested_cents=5000
    )
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()

    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)

    message = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    header_text = message["blocks"][0]["text"]["text"]
    write_up_text = message["blocks"][2]["text"]["text"]
    assert "<!channel>" not in header_text
    assert "<!channel>" not in write_up_text
    assert _ESCAPED_INJECTION in header_text
    assert _ESCAPED_INJECTION in write_up_text


def test_feed_line_escapes_the_project_title(db_session: Session, member: Actor, weave: StubWeaveClient) -> None:
    project = new_project(db_session, member, title=_INJECTION, write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()

    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    feed_message = next(msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED")
    block_text = feed_message["blocks"][0]["text"]["text"]
    assert "<!channel>" not in block_text
    assert _ESCAPED_INJECTION in block_text
    # The top-level fallback text isn't parsed as mrkdwn, so it's left as the raw title.
    assert _INJECTION in feed_message["text"]


def test_notify_decision_escapes_the_title_and_reject_reason(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = new_project(db_session, member, title=_INJECTION, write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)

    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.REJECT,
        reason=_INJECTION,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(revision)

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    all_block_text = " ".join(block["text"]["text"] for block in updated["blocks"] if "text" in block)
    assert "<!channel>" not in all_block_text
    assert _ESCAPED_INJECTION in all_block_text


def test_post_admin_override_escapes_actor_name_and_reason(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")

    slack_notify.post_admin_override(
        db_session,
        slack_client,
        project=project,
        action="admin_withdraw",
        actor_name=_INJECTION,
        reason=_INJECTION,
    )

    posted = next(msg for (chan, _ts), msg in slack_client.messages.items() if chan == project.slack_channel_id)
    block_text = posted["blocks"][0]["text"]["text"]
    assert "<!channel>" not in block_text
    assert _ESCAPED_INJECTION in block_text
    assert _INJECTION in posted["text"]  # fallback text isn't mrkdwn-parsed, so left raw


def test_box_notices_mention_the_owner_in_the_project_channel_once(db_session: Session, member: Actor) -> None:
    from krater.services.compute_jobs import AUDIT_BOX_DESTROY_SOON

    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    member.user.slack_user_id = "U_OWNER"
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    db_session.add(
        AuditEvent(
            actor_id=None,
            action=AUDIT_BOX_DESTROY_SOON,
            project_id=project.id,
            payload={
                "job_id": "00000000-0000-0000-0000-000000000001",
                "job_name": "<!channel> notebook",
                "owner_id": str(member.user.id),
                "destroy_after_hours": 24,
                "destroy_at": "2026-10-12T14:00:00+00:00",
            },
        )
    )
    db_session.flush()

    slack_notify.sync_box_notifications(db_session, slack_client)
    slack_notify.sync_box_notifications(db_session, slack_client)

    assert len(slack_client.messages) == 1
    (message,) = slack_client.messages.values()
    text = message["blocks"][0]["text"]["text"]
    assert "<@U_OWNER>" in text and "2026-10-12 14:00 UTC" in text
    assert "<!channel>" not in text  # the box's name is escaped


def _box_notice(db_session: Session, project, member: Actor, name: str) -> AuditEvent:
    from krater.services.compute_jobs import AUDIT_BOX_STOPPED

    event = AuditEvent(
        actor_id=None,
        action=AUDIT_BOX_STOPPED,
        project_id=project.id,
        payload={"job_id": "j", "job_name": name, "owner_id": str(member.user.id), "destroy_after_hours": 24},
    )
    db_session.add(event)
    db_session.flush()
    return event


def test_a_failing_channel_does_not_hold_up_other_box_notices(db_session: Session, member: Actor) -> None:
    slack_client = FakeSlackClient()
    gone = new_project(db_session, member, title="Gone", write_up="w", budget_requested_cents=5000)
    gone.slack_channel_id = "C_REMOVED"
    fine = new_project(db_session, member, title="Fine", write_up="w", budget_requested_cents=5000)
    fine.slack_channel_id = slack_client.create_channel("ganymede-fine")
    archived = new_project(db_session, member, title="Done", write_up="w", budget_requested_cents=5000)
    archived.slack_channel_id = slack_client.create_channel("ganymede-done")
    archived.slack_channel_archived = True
    _box_notice(db_session, gone, member, "first")
    _box_notice(db_session, fine, member, "second")
    _box_notice(db_session, archived, member, "third")
    real_post = slack_client.post_message

    def post(channel_id, **kwargs):
        if channel_id == "C_REMOVED":
            raise SlackRequestFailedError("channel_not_found")
        return real_post(channel_id, **kwargs)

    slack_client.post_message = post

    slack_notify.sync_box_notifications(db_session, slack_client)

    texts = [m["text"] for m in slack_client.messages.values()]
    assert len(texts) == 1 and "second" in texts[0]  # the archived channel's is dropped, the failing one retried later


def test_someone_who_left_the_team_is_taken_out_of_the_channel(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    teammate = make_actor(slack_user_id="U_TEAMMATE")
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    seat = add_to_team(db_session, project, teammate)
    slack_client = FakeSlackClient()
    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)
    assert "U_TEAMMATE" in slack_client.channels[channel_id]["members"]

    project.members.remove(seat)
    db_session.flush()
    removed = slack_notify.remove_from_channel(
        db_session, slack_client, weave, project=project, user_id=teammate.user.id
    )

    assert removed and "U_TEAMMATE" not in slack_client.channels[channel_id]["members"]
    # Safe to re-run.
    assert slack_notify.remove_from_channel(db_session, slack_client, weave, project=project, user_id=teammate.user.id)


def test_a_reviewer_who_left_the_team_stays_in_the_channel(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    reviewer.user.slack_user_id = "U_REVIEWER"
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    slack_client = FakeSlackClient()
    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)

    removed = slack_notify.remove_from_channel(
        db_session, slack_client, weave, project=project, user_id=reviewer.user.id
    )

    assert not removed and "U_REVIEWER" in slack_client.channels[channel_id]["members"]


def test_someone_back_on_the_team_is_not_taken_out(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    teammate = make_actor(slack_user_id="U_TEAMMATE")
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    add_to_team(db_session, project, teammate)
    slack_client = FakeSlackClient()
    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)

    removed = slack_notify.remove_from_channel(
        db_session, slack_client, weave, project=project, user_id=teammate.user.id
    )

    assert not removed and "U_TEAMMATE" in slack_client.channels[channel_id]["members"]


def test_notify_decision_says_an_unverified_teams_approval_is_awaiting_verification(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    weave.set_minor(member.user.weave_sub, True)
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project, criteria_read=True)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)
    projects.record_review(
        db_session,
        reviewer,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(revision)

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert "Approved · awaiting verification" in updated["text"]


def test_a_transfer_warning_mentions_the_jobs_owner_once(db_session: Session, member: Actor) -> None:
    from krater.services.compute_jobs import AUDIT_TRANSFER_WARNING

    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=10_000)
    member.user.slack_user_id = "U_OWNER"
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    db_session.add(
        AuditEvent(
            actor_id=None,
            action=AUDIT_TRANSFER_WARNING,
            project_id=project.id,
            payload={
                "job_id": "00000000-0000-0000-0000-000000000002",
                "job_name": "train",
                "owner_id": str(member.user.id),
                "level": 1,
                "transfer_cents": 1_100,
                "budget_cents": 10_000,
                "percent": 11,
            },
        )
    )
    db_session.flush()

    slack_notify.sync_box_notifications(db_session, slack_client)
    slack_notify.sync_box_notifications(db_session, slack_client)

    assert len(slack_client.messages) == 1
    (message,) = slack_client.messages.values()
    text = message["blocks"][0]["text"]["text"]
    assert "*Data transfer cost*: <@U_OWNER>" in text
    assert "has cost $11.00 so far, 11% of this project's $100.00 budget" in text
    assert "/compute/00000000-0000-0000-0000-000000000002|Check the job>" in text
