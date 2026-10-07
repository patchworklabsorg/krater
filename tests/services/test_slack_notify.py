"""Channel/message upkeep (`krater.services.slack_notify`), against `FakeSlackClient` -- creation,
invites, idempotency, decision updates, admin overrides, archiving and the periodic reconcile steps.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import AuditEvent, ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import projects, slack_notify
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.slack.errors import SlackRequestFailedError
from krater.slack.fake import FakeSlackClient
from krater.weave import StubWeaveClient


def test_ensure_channel_creates_and_invites_the_team(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    member.user.slack_user_id = "U_MEMBER"
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)

    assert project.slack_channel_id == channel_id
    assert slack_client.channels[channel_id]["name"] == slack_notify.channel_name_for(project)
    assert slack_client.channels[channel_id]["members"] == {"U_MEMBER", "U_REVIEWER"}


def test_ensure_channel_is_idempotent(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_client = FakeSlackClient()

    first = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)
    second = slack_notify.ensure_channel(db_session, slack_client, weave, project=project)

    assert first == second
    assert len(slack_client.channels) == 1


def test_notify_revision_submitted_posts_a_review_message_and_a_feed_line(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(
        db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=5000
    )
    project = projects.submit(db_session, member, project=project)
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
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
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
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="no",
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    projects.update_draft(db_session, member, project=project, write_up="revised")
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    assert revision.number == 2

    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert feed_messages == []


def test_no_feed_line_for_an_amendment(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    projects.start_amendment(db_session, member, project=project)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision

    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id="C_FEED")

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert feed_messages == []


def test_notify_decision_updates_the_message_and_drops_the_buttons(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)

    projects.record_review(
        db_session, reviewer, revision=revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.APPROVED

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert not any(block.get("type") == "actions" for block in updated["blocks"])
    assert "Approved" in updated["text"]


def test_notify_decision_is_a_no_op_while_still_pending(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(db_session, slack_client, weave, revision=revision, feed_channel_id=None)

    before = dict(slack_client.messages)
    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    assert slack_client.messages == before


def test_post_admin_override_posts_to_the_channel(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
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
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()

    slack_notify.post_admin_override(
        db_session, slack_client, project=project, action="admin_withdraw", actor_name="Ana Admin", reason=None
    )

    assert slack_client.messages == {}


def test_archive_project_channel_is_idempotent(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
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
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")

    slack_notify.archive_project_channel(db_session, slack_client, project=project)

    assert project.slack_channel_archived is False
    assert slack_client.channels[project.slack_channel_id]["archived"] is False


def test_sync_reviewer_invites_invites_reviewers_to_open_channels(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    reviewer.user.slack_user_id = "U_NEW_REVIEWER"

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_NEW_REVIEWER" in slack_client.channels[project.slack_channel_id]["members"]


def test_sync_reviewer_invites_skips_archived_channels(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
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
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)

    invited = _team_invites(db_session, weave, project)

    assert invited == {"U_MEMBER", "U_REVIEWER"}
    assert plain_member.user.slack_user_id not in invited


def test_ensure_channel_uses_the_slack_id_weave_reports_for_a_reviewer_krater_has_never_seen(
    db_session: Session, member: Actor, weave: StubWeaveClient
) -> None:
    weave.put_user("PWLNEVERSEEN", name="Nev", email="nev@example.com", roles=["member", "reviewer"], slack_id="U_NEV")
    member.user.slack_user_id = "U_MEMBER"
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)

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
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)

    assert _team_invites(db_session, weave, project, slack_client) == {"U_MEMBER", "U_GUEST"}


def test_ensure_channel_invites_credited_builders(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    builder = make_actor(slack_user_id="U_BUILDER")
    unlinked_builder = make_actor(email="nobody-in-slack@example.com")
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    assert project.current_revision is not None
    project.current_revision.credited_builder_ids = [builder.user.id, unlinked_builder.user.id]
    member.user.slack_user_id = "U_MEMBER"

    assert _team_invites(db_session, weave, project) == {"U_MEMBER", "U_BUILDER"}


def test_ensure_channel_looks_up_unlinked_people_by_verified_email_and_caches_the_id(
    db_session: Session, member: Actor, reviewer: Actor, make_actor, weave: StubWeaveClient
) -> None:
    unverified_reviewer = make_actor(groups=reviewer.groups, email_verified=False)
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    slack_client = FakeSlackClient()
    slack_client.register_email(member.user.email, "U_BY_EMAIL")
    slack_client.register_email(reviewer.user.email, "U_REVIEWER_BY_EMAIL")
    slack_client.register_email(unverified_reviewer.user.email, "U_UNVERIFIED")

    assert _team_invites(db_session, weave, project, slack_client) == {"U_BY_EMAIL", "U_REVIEWER_BY_EMAIL"}
    assert member.user.slack_user_id == "U_BY_EMAIL"
    assert reviewer.user.slack_user_id == "U_REVIEWER_BY_EMAIL"
    assert unverified_reviewer.user.slack_user_id is None


def test_sync_reviewer_invites_sees_a_reviewer_role_weave_granted_after_the_channel_exists(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
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
    broken = projects.create_project(db_session, member, title="Broken", write_up="w", budget_requested_cents=5000)
    fine = projects.create_project(db_session, member, title="Fine", write_up="w", budget_requested_cents=5000)
    slack_client = _OneBrokenChannelSlackClient()
    broken.slack_channel_id = slack_client.create_channel("ganymede-broken")
    fine.slack_channel_id = slack_client.create_channel("ganymede-fine")
    slack_client.broken_channel_id = broken.slack_channel_id
    db_session.flush()
    reviewer.user.slack_user_id = "U_REVIEWER"

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave)

    assert "U_REVIEWER" in slack_client.channels[fine.slack_channel_id]["members"]


def test_sync_budget_notifications_posts_a_warning_once(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
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


def test_sync_missed_archives_archives_finished_projects(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED

    slack_notify.sync_missed_archives(db_session, slack_client)

    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True


def test_reconcile_runs_every_step(db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED
    db_session.flush()
    db_session.commit()

    reviewer.user.slack_user_id = "U_REVIEWER"
    slack_notify.reconcile(db_session, slack_client, weave)

    assert slack_client.channels[project.slack_channel_id]["archived"] is True


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
    project = projects.create_project(
        db_session, member, title=_INJECTION, write_up=_INJECTION, budget_requested_cents=5000
    )
    project = projects.submit(db_session, member, project=project)
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
    project = projects.create_project(db_session, member, title=_INJECTION, write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
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
    project = projects.create_project(db_session, member, title=_INJECTION, write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
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
    )
    db_session.refresh(revision)

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    all_block_text = " ".join(block["text"]["text"] for block in updated["blocks"] if "text" in block)
    assert "<!channel>" not in all_block_text
    assert _ESCAPED_INJECTION in all_block_text


def test_post_admin_override_escapes_actor_name_and_reason(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
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
