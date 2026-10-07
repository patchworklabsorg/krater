"""Slack channel/message upkeep: creating and populating a project's private review channel, updating
the review message once a decision is made, posting admin overrides, archiving finished channels, and
the periodic reconcile pass that catches anything a web request's deferred job missed.

Framework-free (no FastAPI, no procrastinate) so it can be unit-tested directly against
`krater.slack.FakeSlackClient`/`krater.weave.StubWeaveClient` and driven by the worker's task wrappers
(`krater/worker/app.py`) alike -- mirrors `krater.services.skypilot_sync`. Every function here is safe
to call repeatedly: each one re-checks the state it would otherwise duplicate (an already-created
channel, an already-posted message, an already-archived channel, an already-posted budget notification)
before doing anything, so a retried or re-run job never double-posts.

See `docs/SPEC.md` "Slack integration" for the design this implements.
"""

from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    AuditEvent,
    Project,
    ProjectRevision,
    ProjectStatus,
    ReviewDecision,
    RevisionKind,
    RevisionOutcome,
    SlackNotification,
    User,
)
from krater.services.actor import GROUP_REVIEWER
from krater.services.skypilot_sync import AUDIT_BUDGET_TEARDOWN, AUDIT_BUDGET_WARNING
from krater.services.slack_membership import slack_id_for
from krater.slack.client import SlackClient
from krater.slack.errors import SlackError, SlackRequestFailedError
from krater.weave.client import WeaveClient

logger = logging.getLogger(__name__)

#: Slack channel names: lowercase letters, numbers and hyphens, 80 characters max.
_MAX_CHANNEL_NAME_LENGTH = 80
_MAX_WRITE_UP_EXCERPT_LENGTH = 400


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "project"


def channel_name_for(project: Project) -> str:
    """The Slack channel name for `project`: `ganymede-<slug>-<short id>`, lowercase, within Slack's
    80-character limit. The short id keeps it unique even if two projects share a title/slug."""
    short_id = project.id.hex[:8]
    slug = _slugify(project.title)[: _MAX_CHANNEL_NAME_LENGTH - len("ganymede--") - len(short_id)]
    return f"ganymede-{slug}-{short_id}"


def _escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack's mrkdwn parser treats specially, so a project title,
    write-up, reject reason or Weave display name containing `<!channel>`, `<!here>`, a user/channel
    mention (`<@U…>`/`<#C…>`), or a link-hijack (`<https://phish|Approve>`) renders as inert literal
    text in a block's `mrkdwn`-typed `text` field instead of being interpreted by Slack. Order matters:
    `&` must be escaped first, or escaping `<`/`>` afterwards would double-escape the `&` this
    introduces into `&lt;`/`&gt;`. Only for `mrkdwn` text -- a message's own top-level `text` fallback
    (used for notifications/previews) isn't parsed as mrkdwn, so it's left as-is."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_cents(cents: int) -> str:
    """A tiny, Slack-message-only formatter -- deliberately not importing `krater.web.money` (services
    don't depend on the web layer; see CLAUDE.md)."""
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{remainder:02d}"


def _slack_ids(session: Session, slack_client: SlackClient, people: list[User]) -> set[str]:
    """Slack ids for everyone in `people` who has a Slack account Krater can find (`slack_id_for`: the
    stored id, else an email lookup whose result is cached on the user). Nobody is filtered by guest
    status here: the live client invites with `force` and skips anyone Slack refuses (see
    `LiveSlackClient.invite_users`), so one guest never blocks the rest."""
    ids: set[str] = set()
    for person in people:
        slack_id = slack_id_for(session, slack_client, person)
        if slack_id:
            ids.add(slack_id)
    return ids


def _reviewer_slack_ids(session: Session, slack_client: SlackClient, weave_client: WeaveClient) -> set[str]:
    """Slack ids for every active user Weave says holds `ganymede:reviewer`: Weave's `slack_id`, else
    the Krater user row's (`slack_id_for`), else a Slack lookup by an email Weave says is verified."""
    ids: set[str] = set()
    for reviewer in weave_client.list_users_with_role(GROUP_REVIEWER):
        if not reviewer.active:
            continue
        slack_id = reviewer.slack_id
        if not slack_id:
            user = session.scalars(sa.select(User).where(User.weave_sub == reviewer.sub)).first()
            if user is not None:
                slack_id = slack_id_for(session, slack_client, user)
            elif reviewer.email_verified and reviewer.email:
                slack_id = slack_client.lookup_user_by_email(reviewer.email)
        if slack_id:
            ids.add(slack_id)
    return ids


def _team_slack_ids(
    session: Session, slack_client: SlackClient, weave_client: WeaveClient, *, project: Project
) -> list[str]:
    """The submitter, the current revision's credited builders, and every current Ganymede reviewer --
    everyone `docs/SPEC.md` says should be in a project's channel -- resolved to Slack user ids."""
    people = [project.submitter]
    revision = project.current_revision
    if revision is not None and revision.credited_builder_ids:
        people.extend(session.scalars(sa.select(User).where(User.id.in_(revision.credited_builder_ids))))
    ids = _slack_ids(session, slack_client, people) | _reviewer_slack_ids(session, slack_client, weave_client)
    return sorted(ids)


def ensure_channel(session: Session, slack_client: SlackClient, weave_client: WeaveClient, *, project: Project) -> str:
    """Ensure `project` has a Slack channel, invite the current team to it, and return the channel id.

    Idempotent: creates the channel only if `project.slack_channel_id` is unset. Always (re-)invites the
    current team -- cheap, and safe to re-run (`docs/SPEC.md`: "safe to retry: already_in_channel counts
    as success") -- so a team change (a new reviewer, a newly-credited builder) is picked up every call.
    """
    if project.slack_channel_id is None:
        channel_id = slack_client.create_channel(channel_name_for(project))
        project.slack_channel_id = channel_id
        session.flush()

    team_ids = _team_slack_ids(session, slack_client, weave_client, project=project)
    if team_ids:
        slack_client.invite_users(project.slack_channel_id, team_ids)
    return project.slack_channel_id


def _stage_label(revision: ProjectRevision) -> str:
    if revision.kind is RevisionKind.COMPLETION:
        return "Completion review"
    if revision.kind is RevisionKind.AMENDMENT:
        return "Amendment review"
    return "Proposal review"


def _write_up_excerpt(write_up: str) -> str:
    write_up = write_up.strip()
    if len(write_up) <= _MAX_WRITE_UP_EXCERPT_LENGTH:
        return write_up
    return write_up[:_MAX_WRITE_UP_EXCERPT_LENGTH].rstrip() + "…"


def _review_message(project: Project, revision: ProjectRevision) -> tuple[list[dict], str]:
    """The Approve/Reject review message for `revision`. The buttons' `value` carries the revision id,
    per `docs/SPEC.md`, so `/slack/interactions` knows which revision a click is about."""
    stage = _stage_label(revision)
    header = f"*{stage}: {_escape_mrkdwn(project.title)}* (revision {revision.number})"
    # The message's top-level `text` (notification/preview fallback) isn't parsed as mrkdwn, so the raw
    # title is fine here -- see `_escape_mrkdwn`'s docstring.
    text = f"{stage}: {project.title} (revision {revision.number})"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Requested budget:*\n{_format_cents(revision.budget_requested_cents)}"}
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _escape_mrkdwn(_write_up_excerpt(revision.write_up))}},
        {
            "type": "actions",
            "block_id": "review_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": str(revision.id),
                },
                {
                    "type": "button",
                    "action_id": "reject",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": str(revision.id),
                },
            ],
        },
    ]
    return blocks, text


def notify_revision_submitted(
    session: Session,
    slack_client: SlackClient,
    weave_client: WeaveClient,
    *,
    revision: ProjectRevision,
    feed_channel_id: str | None,
) -> None:
    """Ensure the project's channel exists, invite the team, and post the review message for a just-
    submitted revision -- proposal, amendment or completion alike. Posts one line to the feed channel
    only for a brand-new project's first proposal (`docs/SPEC.md`: "for new proposals").

    Idempotent: does nothing beyond `ensure_channel`'s own idempotent invite if this revision's message
    has already been posted (`revision.slack_message_ts` set).
    """
    project = revision.project
    channel_id = ensure_channel(session, slack_client, weave_client, project=project)

    if revision.slack_message_ts is not None:
        return

    blocks, text = _review_message(project, revision)
    ts = slack_client.post_message(channel_id, blocks=blocks, text=text)
    revision.slack_message_channel_id = channel_id
    revision.slack_message_ts = ts
    session.flush()

    if feed_channel_id and revision.kind is RevisionKind.PROPOSAL and revision.number == 1:
        feed_block_text = f"New Ganymede proposal: *{_escape_mrkdwn(project.title)}*"
        feed_fallback_text = f"New Ganymede proposal: {project.title}"
        slack_client.post_message(
            feed_channel_id,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": feed_block_text}}],
            text=feed_fallback_text,
        )


def _outcome_label(revision: ProjectRevision) -> str:
    if revision.outcome is RevisionOutcome.APPROVED:
        return ":white_check_mark: Approved"
    if revision.outcome is RevisionOutcome.REJECTED:
        return ":x: Rejected"
    return revision.outcome.value  # pragma: no cover -- superseded/pending never reach here


def _latest_reject_reason(revision: ProjectRevision) -> str | None:
    rejecting = [review for review in revision.reviews if review.decision is ReviewDecision.REJECT]
    if not rejecting:
        return None
    return max(rejecting, key=lambda review: review.created_at).reason


def notify_decision(session: Session, slack_client: SlackClient, *, revision: ProjectRevision) -> None:
    """Update `revision`'s review message to show its outcome, with the Approve/Reject buttons removed
    (Slack has no per-button disabled state; dropping the `actions` block is the equivalent).

    Idempotent, and safe to call for any revision: a no-op unless it has both a message to update
    (`slack_message_channel_id`/`slack_message_ts` set) and a resolved outcome.
    """
    if revision.slack_message_channel_id is None or revision.slack_message_ts is None:
        return
    if revision.outcome is RevisionOutcome.PENDING:
        return

    project = revision.project
    stage = _stage_label(revision)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{stage}: {_escape_mrkdwn(project.title)}* (revision {revision.number})",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Outcome:* {_outcome_label(revision)}"}},
    ]
    reason = _latest_reject_reason(revision)
    if reason:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Reason:* {_escape_mrkdwn(reason)}"}})
    # Top-level fallback text isn't parsed as mrkdwn -- the raw title/reason are fine here.
    text = f"{stage} for {project.title}: {_outcome_label(revision)}"

    slack_client.update_message(revision.slack_message_channel_id, revision.slack_message_ts, blocks=blocks, text=text)


def post_admin_override(
    session: Session,
    slack_client: SlackClient,
    *,
    project: Project,
    action: str,
    actor_name: str,
    reason: str | None,
    extra: str | None = None,
) -> None:
    """Post an admin-override notice to `project`'s channel, per `docs/SPEC.md` "Admin overrides"
    ("Overrides are posted in the project's channel so reviewers can see them"). A no-op if the project
    has no channel yet (e.g. an admin withdrawing a project still in `draft`). Takes `session` for
    signature symmetry with the rest of this module, though there's nothing to persist here."""
    if project.slack_channel_id is None:
        return

    block_lines = [f":rotating_light: Admin override by *{_escape_mrkdwn(actor_name)}*: `{action}`"]
    fallback_lines = [f"Admin override by {actor_name}: {action}"]
    if extra:
        block_lines.append(_escape_mrkdwn(extra))
        fallback_lines.append(extra)
    if reason:
        block_lines.append(f"*Reason:* {_escape_mrkdwn(reason)}")
        fallback_lines.append(f"Reason: {reason}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(block_lines)}}]
    slack_client.post_message(project.slack_channel_id, blocks=blocks, text="\n".join(fallback_lines))


def archive_project_channel(session: Session, slack_client: SlackClient, *, project: Project) -> None:
    """Archive `project`'s channel once it's `completed`/`withdrawn`. Idempotent: a no-op if the project
    isn't actually terminal, has no channel, or is already marked archived -- so this is safe to defer
    unconditionally after every decision/withdrawal, and to re-run from the periodic reconcile."""
    if project.status not in (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN):
        return
    if project.slack_channel_id is None or project.slack_channel_archived:
        return
    slack_client.archive_channel(project.slack_channel_id)
    project.slack_channel_archived = True
    session.flush()


# --------------------------------------------------------------------------------------------------
# Periodic reconcile: invite newly-added reviewers, post missed budget notifications, catch missed
# archives. See `krater/worker/app.py`'s `slack_reconcile` periodic task.
# --------------------------------------------------------------------------------------------------


def sync_reviewer_invites(session: Session, slack_client: SlackClient, weave_client: WeaveClient) -> None:
    """Invite every current Ganymede reviewer to every open (non-archived) project channel.

    `docs/SPEC.md`: "When someone gets the reviewer role in Weave, they get invited to open project
    channels by a periodic job". Reviewers are the active users Weave's directory lists with the role.
    Safe to re-run: inviting an existing member is a no-op (see `SlackClient.invite_users`). A channel
    Slack refuses (archived or left by hand, say) is logged and skipped so it doesn't hold up every
    other channel.
    """
    reviewer_ids = _reviewer_slack_ids(session, slack_client, weave_client)
    if not reviewer_ids:
        return

    stmt = sa.select(Project).where(Project.slack_channel_id.is_not(None), Project.slack_channel_archived.is_(False))
    for project in session.scalars(stmt):
        try:
            slack_client.invite_users(project.slack_channel_id, sorted(reviewer_ids))
        except SlackRequestFailedError:
            logger.exception("krater.slack reviewer invite failed for project %s; continuing", project.id)


def _budget_event_message(event: AuditEvent) -> tuple[list[dict], str]:
    payload = event.payload or {}
    if event.action == AUDIT_BUDGET_TEARDOWN:
        text = (
            ":octagonal_sign: *Budget teardown*: this project's clusters and managed jobs have been "
            f"torn down. Ceiling: {_format_cents(payload.get('ceiling_cents', 0))}, "
            f"spend: {_format_cents(payload.get('spend_cents', 0))}."
        )
    else:
        text = (
            f":warning: *Budget warning*: this project has used {payload.get('percent', 0)}% of its ceiling "
            f"({_format_cents(payload.get('spend_cents', 0))} of {_format_cents(payload.get('ceiling_cents', 0))})."
        )
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text


def sync_budget_notifications(session: Session, slack_client: SlackClient) -> None:
    """Post every not-yet-posted `budget_warning`/`budget_teardown` `AuditEvent` to its project's
    channel, and record it in `SlackNotification` so it's never posted twice.

    These events are written by `krater.services.skypilot_sync.enforce_budgets` with `actor=None` (a
    system process, not a request), so nothing calls this synchronously the way `notify_decision`/
    `post_admin_override` are -- the periodic reconcile is the only thing that posts them.
    """
    already_posted = sa.select(SlackNotification.audit_event_id)
    stmt = sa.select(AuditEvent).where(
        AuditEvent.action.in_((AUDIT_BUDGET_WARNING, AUDIT_BUDGET_TEARDOWN)),
        AuditEvent.project_id.is_not(None),
        AuditEvent.id.not_in(already_posted),
    )
    for event in session.scalars(stmt):
        project = session.get(Project, event.project_id)
        if project is None or project.slack_channel_id is None:
            # Nothing to post to yet -- try again next reconcile tick once a channel exists.
            continue
        blocks, text = _budget_event_message(event)
        slack_client.post_message(project.slack_channel_id, blocks=blocks, text=text)
        session.add(SlackNotification(audit_event_id=event.id))
        session.flush()


def sync_missed_archives(session: Session, slack_client: SlackClient) -> None:
    """Archive the channel of any `completed`/`withdrawn` project whose immediate archive (deferred from
    the web request/Slack action that finished it) never ran -- `docs/SPEC.md`: "archive channels of
    finished projects that were missed"."""
    stmt = sa.select(Project).where(
        Project.status.in_((ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)),
        Project.slack_channel_id.is_not(None),
        Project.slack_channel_archived.is_(False),
    )
    for project in session.scalars(stmt):
        archive_project_channel(session, slack_client, project=project)


def reconcile(session: Session, slack_client: SlackClient, weave_client: WeaveClient) -> None:
    """Run every periodic Slack step in order, committing after each so one step's `SlackError` doesn't
    lose the others' work (mirrors `krater.services.skypilot_sync.reconcile`)."""
    steps = (
        ("sync_reviewer_invites", lambda: sync_reviewer_invites(session, slack_client, weave_client)),
        ("sync_budget_notifications", lambda: sync_budget_notifications(session, slack_client)),
        ("sync_missed_archives", lambda: sync_missed_archives(session, slack_client)),
    )
    for name, step in steps:
        try:
            step()
        except SlackError:
            logger.exception("krater.slack reconcile step %s failed; continuing", name)
            session.rollback()
        else:
            session.commit()


__all__ = [
    "AUDIT_BUDGET_TEARDOWN",
    "AUDIT_BUDGET_WARNING",
    "archive_project_channel",
    "channel_name_for",
    "ensure_channel",
    "notify_decision",
    "notify_revision_submitted",
    "post_admin_override",
    "reconcile",
    "sync_budget_notifications",
    "sync_missed_archives",
    "sync_reviewer_invites",
]
