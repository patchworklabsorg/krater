"""Slack channel/message upkeep: creating and populating a project's private review channel, updating
the review message once a decision is made, posting admin overrides, archiving finished channels, and
the periodic reconcile pass that catches anything a web request's deferred job missed.

Framework-free (no FastAPI, no procrastinate) so it can be unit-tested directly against
`krater.slack.FakeSlackClient`/`krater.weave.StubWeaveClient` and driven by the worker's task wrappers
(`krater/worker/app.py`) alike. Every function here is safe
to call repeatedly: each one re-checks the state it would otherwise duplicate (an already-created
channel, an already-posted message, an already-archived channel, an already-posted budget notification)
before doing anything, so a retried or re-run job never double-posts.

See `docs/SPEC.md` "Slack integration" for the design this implements.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import (
    AuditEvent,
    FundingHold,
    Project,
    ProjectRevision,
    ProjectStatus,
    ReviewDecision,
    RevisionKind,
    RevisionOutcome,
    SlackNotification,
    User,
)
from krater.services import funding_holds, grants, ospool_grants, teams
from krater.services.actor import GROUP_REVIEWER
from krater.services.budget_alerts import AUDIT_BUDGET_TEARDOWN, AUDIT_BUDGET_WARNING
from krater.services.compute_jobs import (
    AUDIT_BOX_DESTROY_SOON,
    AUDIT_BOX_DESTROYED,
    AUDIT_BOX_STOPPED,
    AUDIT_TRANSFER_WARNING,
)
from krater.services.grants import AUDIT_GRANT_HELD, AUDIT_GRANT_RELEASED
from krater.services.slack_membership import slack_id_for
from krater.slack.client import SlackClient
from krater.slack.errors import SlackError, SlackRequestFailedError
from krater.weave import links as weave_links
from krater.weave.client import WeaveClient
from krater.weave.errors import WeaveUnavailableError

logger = logging.getLogger(__name__)

#: Slack channel names: lowercase letters, numbers and hyphens, 80 characters max.
_MAX_CHANNEL_NAME_LENGTH = 80
_MAX_WRITE_UP_EXCERPT_LENGTH = 400
#: How long a submission's own deferred job has to post its review message before the reconcile posts it. Without
#: the wait, a reconcile run that starts while that job is still running could post the message twice.
MISSED_REVIEW_MESSAGE_GRACE = timedelta(minutes=10)
#: The oldest submission the reconcile posts a missed review message for.
MISSED_REVIEW_MESSAGE_WINDOW = timedelta(days=7)


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


def _funding_hold_label(value: str | None) -> str | None:
    """A held grant's `funding_hold` payload value as the hold reads in a sentence ("awaiting funding"), or `None`
    when the grant waits on verification. An unknown value still reads as a hold, never as verification."""
    if not value:
        return None
    try:
        return funding_holds.LABELS[FundingHold(value)].lower()
    except ValueError:
        return "on a funding hold"


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
    """Slack ids for every active user Weave says holds `ganymede:reviewer`: Weave's `slack_id`, else the
    one stored on their Krater user row."""
    ids: set[str] = set()
    for reviewer in weave_client.list_users_with_role(GROUP_REVIEWER):
        if not reviewer.active:
            continue
        slack_id = reviewer.slack_id
        if not slack_id:
            user = session.scalars(sa.select(User).where(User.weave_sub == reviewer.sub)).first()
            if user is not None:
                slack_id = slack_id_for(session, slack_client, user)
        if slack_id:
            ids.add(slack_id)
    return ids


def _team_slack_ids(
    session: Session, slack_client: SlackClient, weave_client: WeaveClient, *, project: Project
) -> list[str]:
    """The project's team (its lead and accepted members) and every current Ganymede reviewer -- everyone
    `docs/SPEC.md` says should be in a project's channel -- resolved to Slack user ids."""
    people = list(session.scalars(sa.select(User).where(User.id.in_(teams.team_user_ids(project)))))
    ids = _slack_ids(session, slack_client, people) | _reviewer_slack_ids(session, slack_client, weave_client)
    return sorted(ids)


def ensure_channel(session: Session, slack_client: SlackClient, weave_client: WeaveClient, *, project: Project) -> str:
    """Ensure `project` has a Slack channel, invite the current team to it, and return the channel id.

    Idempotent: creates the channel only if `project.slack_channel_id` is unset. Always (re-)invites the
    current team -- cheap, and safe to re-run (`docs/SPEC.md`: "safe to retry: already_in_channel counts
    as success") -- so a team change (a new reviewer, a teammate who just joined) is picked up every call.
    """
    if project.slack_channel_id is None:
        channel_id = slack_client.create_channel(channel_name_for(project))
        project.slack_channel_id = channel_id
        session.flush()

    team_ids = _team_slack_ids(session, slack_client, weave_client, project=project)
    if team_ids:
        slack_client.invite_users(project.slack_channel_id, team_ids)
    return project.slack_channel_id


def remove_from_channel(
    session: Session, slack_client: SlackClient, weave_client: WeaveClient, *, project: Project, user_id: uuid.UUID
) -> bool:
    """Take someone who left `project`'s team, or was removed from it, out of its channel, which holds the project's
    review and budget talk. Not while they still belong there: back on the team, or a current reviewer (reviewers are
    in every open channel). Returns whether Slack was asked to remove them; safe to re-run."""
    if project.slack_channel_id is None or project.slack_channel_archived:
        return False
    if user_id in teams.team_user_ids(project):
        return False
    user = session.get(User, user_id)
    slack_id = slack_id_for(session, slack_client, user) if user is not None else None
    if not slack_id or slack_id in _reviewer_slack_ids(session, slack_client, weave_client):
        return False
    slack_client.remove_user(project.slack_channel_id, slack_id)
    return True


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


def _budget_fields(revision: ProjectRevision) -> list[dict]:
    """The amounts on a review message: what approval funds, and for a proposal or amendment the finished product's
    rough total, which approval doesn't fund."""
    label = "Proof of concept" if revision.kind is RevisionKind.PROPOSAL else "Requested budget"
    fields = [{"type": "mrkdwn", "text": f"*{label}:*\n{_format_cents(revision.budget_requested_cents)}"}]
    if revision.kind is not RevisionKind.COMPLETION and revision.full_budget_cents:
        amount = _format_cents(revision.full_budget_cents)
        fields.append({"type": "mrkdwn", "text": f"*Finished product (not requested yet):*\n~{amount}"})
    return fields


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
        {"type": "section", "fields": _budget_fields(revision)},
        {
            "type": "section",
            # The application's one-sentence summary, when it has one: what reviewers should read first.
            "text": {
                "type": "mrkdwn",
                "text": _escape_mrkdwn(_write_up_excerpt(revision.summary or revision.write_up)),
            },
        },
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
    if revision.kind is not RevisionKind.COMPLETION:
        # Above the buttons: they stay the message's last block.
        link = f"{get_settings().base_url.rstrip('/')}/projects/{project.id}"
        note = f"Approve here grants the full amount. To grant less, approve on <{link}|the project page>."
        blocks.insert(-1, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
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
    # Lock the revision row until the caller commits, so the submission's own job and the reconcile can't both see
    # no message and both post one.
    session.refresh(revision, with_for_update=True)
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
        try:
            slack_client.post_message(
                feed_channel_id,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": feed_block_text}}],
                text=feed_fallback_text,
            )
        except SlackError:
            # The review message is in Slack already. Raising would roll back its ts, and a retry would post it
            # again. The feed line only gives visibility, so log it and go on.
            logger.exception("krater.slack feed line for revision %s failed; not retried", revision.id)


def _outcome_label(revision: ProjectRevision, hold: str | None = None) -> str:
    if revision.outcome is RevisionOutcome.APPROVED:
        if hold is not None and revision.kind is not RevisionKind.COMPLETION:
            # Approved, but its budget waits: on an admin clearing a funding hold, or on Weave clearing the team.
            return f":white_check_mark: Approved · {hold.lower()}"
        return ":white_check_mark: Approved"
    if revision.outcome is RevisionOutcome.REJECTED:
        return ":x: Rejected"
    return revision.outcome.value  # pragma: no cover -- superseded/pending never reach here


def _latest_reject_reason(revision: ProjectRevision) -> str | None:
    rejecting = [review for review in revision.reviews if review.decision is ReviewDecision.REJECT]
    if not rejecting:
        return None
    return max(rejecting, key=lambda review: review.created_at).reason


def _grant_reason(revision: ProjectRevision) -> str | None:
    """Why the approvers granted less: the reason on the approval that chose the amount granted. An admin override
    keeps its reason in the audit log and posts it as its own notice, so there is none here then."""
    choosing = [
        review
        for review in revision.reviews
        if review.decision is ReviewDecision.APPROVE and review.budget_cents == revision.budget_approved_cents
    ]
    if not choosing:
        return None
    return max(choosing, key=lambda review: review.created_at).reason


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
    hold = funding_holds.approval_label(
        project,
        verification_held=grants.pending(session, project) is not None,
        ospool=ospool_grants.for_project(session, project) is not None,
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{stage}: {_escape_mrkdwn(project.title)}* (revision {revision.number})",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Outcome:* {_outcome_label(revision, hold)}"}},
    ]
    reason = _latest_reject_reason(revision)
    granted = None
    if revision.outcome is RevisionOutcome.APPROVED and revision.granted_less:
        what = "a new total of " if revision.kind is RevisionKind.AMENDMENT else ""
        granted = (
            f"Approved for {what}{_format_cents(revision.budget_approved_cents)} of the "
            f"{_format_cents(revision.budget_requested_cents)} asked for."
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Budget:* {granted}"}})
        reason = _grant_reason(revision)
    if reason:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Reason:* {_escape_mrkdwn(reason)}"}})
    # Top-level fallback text isn't parsed as mrkdwn -- the raw title/reason are fine here.
    text = f"{stage} for {project.title}: {_outcome_label(revision, hold)}"
    if granted:
        text = f"{text}. {granted}"

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
    if event.action == AUDIT_GRANT_HELD:
        link = f"{get_settings().base_url.rstrip('/')}/projects/{event.project_id}"
        amount = _format_cents(payload.get("amount_cents", 0))
        hold = _funding_hold_label(payload.get("funding_hold"))
        if hold:
            # A funding hold holds every grant whatever the team's verification (`grants._not_cleared`), and Weave
            # isn't asked, so this says nothing about verification.
            text = (
                f":hourglass_flowing_sand: *Budget held*: {amount} of budget is held because the project is {hold}. "
                f"It's added once an admin clears the hold. <{link}|See the project>."
            )
        else:
            text = (
                f":hourglass_flowing_sand: *Budget held*: {amount} of budget is held until every team member is "
                "verified in Weave (ID verification, and a verified guardian for anyone under 18). "
                f"<{link}|See who it's waiting on>."
            )
    elif event.action == AUDIT_GRANT_RELEASED:
        text = (
            ":white_check_mark: *Budget released*: every team member is verified in Weave, so the held "
            f"{_format_cents(payload.get('amount_cents', 0))} is now this project's to use."
        )
    elif event.action == AUDIT_BUDGET_TEARDOWN:
        text = (
            ":octagonal_sign: *Budget teardown*: this project's machines have been "
            f"torn down. Ceiling: {_format_cents(payload.get('ceiling_cents', 0))}, "
            f"spend: {_format_cents(payload.get('spend_cents', 0))}."
        )
    elif payload.get("final"):
        text = (
            f":rotating_light: *Final budget warning*: this project has used {payload.get('percent', 0)}% of its "
            f"ceiling ({_format_cents(payload.get('spend_cents', 0))} of "
            f"{_format_cents(payload.get('ceiling_cents', 0))}). Krater stops every job and machine just before 100%. "
            "Save your work now."
        )
    else:
        text = (
            f":warning: *Budget warning*: this project has used {payload.get('percent', 0)}% of its ceiling "
            f"({_format_cents(payload.get('spend_cents', 0))} of {_format_cents(payload.get('ceiling_cents', 0))})."
        )
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text


def _held_grant_dm(project: Project) -> tuple[list[dict], str]:
    """The direct message to a team member a held grant waits on. The same words for everyone: Krater doesn't keep
    why they aren't cleared (it can say they're under 18), and Weave's page tells them what's left."""
    base = get_settings().base_url.rstrip("/")
    title = _escape_mrkdwn(project.title or "your project")
    verify = weave_links.verification_url()
    where = f"<{verify}|Weave's verification page>" if verify else "Weave's verification page"
    text = (
        f":hourglass_flowing_sand: *<{base}/projects/{project.id}|{title}>* is approved, but its budget is held until "
        "every team member is verified in Weave, you included. That's ID verification, and for anyone under 18 a "
        f"verified guardian. See what's left on {where}. Krater adds the budget within a few minutes of the last check "
        "passing."
    )
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text


def _dm_waiting_members(session: Session, slack_client: SlackClient, event: AuditEvent, project: Project) -> None:
    """DM each team member a held grant waits on (`waiting_on`, Patchwork IDs). A member without a Slack id, or one
    Slack refuses, is skipped and logged: the channel post and the project page still say it."""
    subs = (event.payload or {}).get("waiting_on") or []
    if not subs:
        return
    blocks, text = _held_grant_dm(project)
    for user in session.scalars(sa.select(User).where(User.weave_sub.in_(subs))):
        slack_id = slack_id_for(session, slack_client, user)
        if not slack_id:
            continue
        try:
            slack_client.post_message(slack_id, blocks=blocks, text=text)
        except SlackRequestFailedError:
            logger.exception("krater.slack held-grant DM to %s for project %s failed; continuing", user.id, project.id)


def sync_budget_notifications(session: Session, slack_client: SlackClient) -> None:
    """Post every not-yet-posted `budget_warning`/`budget_teardown` `AuditEvent`, and every held or released
    budget grant (`krater.services.grants`), to its project's channel, and record it in `SlackNotification` so it's
    never posted twice. A held grant also DMs each team member it waits on.

    These events are written by `krater.services.budget_alerts` with `actor=None` (a
    system process, not a request), so nothing calls this synchronously the way `notify_decision`/
    `post_admin_override` are: the periodic reconcile and the compute scheduler task post them. The caller commits.
    """
    # Both callers can run at once; the lock (held until the caller commits) keeps them from posting one event twice.
    session.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('krater.budget_notifications'))"))
    already_posted = sa.select(SlackNotification.audit_event_id)
    stmt = (
        sa.select(AuditEvent)
        .where(
            AuditEvent.action.in_(
                (AUDIT_BUDGET_WARNING, AUDIT_BUDGET_TEARDOWN, AUDIT_GRANT_HELD, AUDIT_GRANT_RELEASED)
            ),
            AuditEvent.project_id.is_not(None),
            AuditEvent.id.not_in(already_posted),
        )
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    for event in list(session.scalars(stmt)):
        project = session.get(Project, event.project_id)
        if project is None or project.slack_channel_id is None:
            # Nothing to post to yet -- try again next reconcile tick once a channel exists.
            continue
        if project.slack_channel_archived:
            # The project finished; nobody reads its channel. Mark it handled so it isn't retried forever.
            session.add(SlackNotification(audit_event_id=event.id))
            session.flush()
            continue
        blocks, text = _budget_event_message(event)
        try:
            slack_client.post_message(project.slack_channel_id, blocks=blocks, text=text)
        except SlackRequestFailedError:
            # One channel Slack refuses (archived by hand, bot kicked) mustn't hold up every other notice.
            logger.exception("krater.slack budget notice %s for project %s failed; continuing", event.id, project.id)
            continue
        if event.action == AUDIT_GRANT_HELD:
            # After the channel post, so a retried event doesn't DM twice.
            _dm_waiting_members(session, slack_client, event, project)
        session.add(SlackNotification(audit_event_id=event.id))
        session.flush()


#: Notices about one member's job or box, posted to its project's channel mentioning its owner.
BOX_NOTICES = (AUDIT_BOX_STOPPED, AUDIT_BOX_DESTROY_SOON, AUDIT_BOX_DESTROYED, AUDIT_TRANSFER_WARNING)


def _box_message(session: Session, slack_client: SlackClient, event: AuditEvent, project: Project):
    payload = event.payload or {}
    owner = session.get(User, uuid.UUID(payload["owner_id"])) if payload.get("owner_id") else None
    slack_id = slack_id_for(session, slack_client, owner) if owner is not None else None
    # Without a Slack id there is no name to fall back on: Krater stores none (docs/weave-integration.md).
    who = f"<@{slack_id}>" if slack_id else "Box owner"
    name = _escape_mrkdwn(payload.get("job_name", "a box"))
    link = f"{get_settings().base_url.rstrip('/')}/projects/{project.id}/compute/{payload.get('job_id', '')}"
    hours = payload.get("destroy_after_hours")
    if event.action == AUDIT_TRANSFER_WARNING:
        text = (
            f":warning: *Data transfer cost*: {who}, data in and out of *{name}*'s machine has cost "
            f"{_format_cents(payload.get('transfer_cents', 0))} so far, {payload.get('percent', 0)}% of this "
            f"project's {_format_cents(payload.get('budget_cents', 0))} budget. Vast bills it per GB on top of the "
            f"hourly price. <{link}|Check the job>, and cancel it if that's more than you expected."
        )
    elif event.action == AUDIT_BOX_STOPPED:
        saved = "/workspace is saved" if payload.get("synced") else "/workspace may not have saved"
        text = (
            f":zzz: *Box stopped*: {who}, your box *{name}* stopped ({_escape_mrkdwn(payload.get('reason', ''))}); "
            f"{saved}. <{link}|Resume it> within {hours} hours, or Krater destroys it."
        )
    elif event.action == AUDIT_BOX_DESTROY_SOON:
        at = payload.get("destroy_at", "")[:16].replace("T", " ")
        text = (
            f":hourglass_flowing_sand: *Box destroyed soon*: {who}, your stopped box *{name}* will be destroyed at "
            f"{at} UTC unless you <{link}|resume it>."
        )
    else:
        text = (
            f":wastebasket: *Box destroyed*: {who}, your box *{name}* was stopped for {hours} hours and is gone. "
            "Its last saved /workspace is kept."
        )
    plain = re.sub(r"<[^|>]*\|([^>]*)>", r"\1", text)  # links as their words, for the notification
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], plain


def sync_box_notifications(session: Session, slack_client: SlackClient) -> None:
    """Post every not-yet-posted box notice (stopped by Krater, destroyed soon, destroyed) and data transfer warning
    to its project's channel, mentioning the box's or job's owner, once each (`SlackNotification`)."""
    already_posted = sa.select(SlackNotification.audit_event_id)
    stmt = sa.select(AuditEvent).where(
        AuditEvent.action.in_(BOX_NOTICES),
        AuditEvent.project_id.is_not(None),
        AuditEvent.id.not_in(already_posted),
    )
    for event in list(session.scalars(stmt)):
        project = session.get(Project, event.project_id)
        if project is None or project.slack_channel_id is None:
            continue  # no channel yet: try again next tick
        if project.slack_channel_archived:
            # The project finished; nobody reads its channel. Mark it handled so it isn't retried forever.
            session.add(SlackNotification(audit_event_id=event.id))
            session.flush()
            continue
        blocks, text = _box_message(session, slack_client, event, project)
        try:
            slack_client.post_message(project.slack_channel_id, blocks=blocks, text=text)
        except SlackRequestFailedError:
            # One channel Slack refuses (removed, bot kicked) mustn't hold up every other notice: retried next tick.
            logger.exception("krater.slack box notice %s for project %s failed; continuing", event.id, project.id)
            continue
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


def sync_missed_review_messages(
    session: Session,
    slack_client: SlackClient,
    weave_client: WeaveClient,
    *,
    feed_channel_id: str | None,
    now: datetime | None = None,
) -> None:
    """Post the review message of any submitted, still-pending revision that has none, because the job deferred by
    its submission failed (a bad bot token, Slack down). Runs `notify_revision_submitted`, so the channel, the team
    invite and the feed line for a new project's first proposal come with it.

    Only revisions submitted between `MISSED_REVIEW_MESSAGE_WINDOW` and `MISSED_REVIEW_MESSAGE_GRACE` ago: a newer one
    is left to its own job, and an older one is not posted out of the blue. Each revision is committed on its own, and
    one that Slack refuses (an archived channel, the bot removed) doesn't hold up the others; the step raises at the
    end so the run still counts towards the `slack` alert."""
    now = now or datetime.now(UTC)
    stmt = (
        sa.select(ProjectRevision.id)
        .join(Project, ProjectRevision.project_id == Project.id)
        .where(
            ProjectRevision.outcome == RevisionOutcome.PENDING,
            ProjectRevision.submitted_at < now - MISSED_REVIEW_MESSAGE_GRACE,
            ProjectRevision.submitted_at > now - MISSED_REVIEW_MESSAGE_WINDOW,
            ProjectRevision.slack_message_ts.is_(None),
            Project.status.not_in((ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)),
        )
        .order_by(ProjectRevision.submitted_at)
    )
    failures: list[SlackError] = []
    for revision_id in session.scalars(stmt).all():
        revision = session.get(ProjectRevision, revision_id)
        try:
            # Re-check under the lock `notify_revision_submitted` takes: a decision made since the select must not get
            # a message with live buttons.
            locked = session.scalars(
                sa.select(ProjectRevision)
                .where(ProjectRevision.id == revision_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).one_or_none()
            if locked is None or locked.outcome is not RevisionOutcome.PENDING or locked.slack_message_ts is not None:
                session.rollback()
                continue
            notify_revision_submitted(
                session, slack_client, weave_client, revision=revision, feed_channel_id=feed_channel_id
            )
            session.commit()
        except SlackError as exc:
            logger.exception("krater.slack missed review message for revision %s failed; continuing", revision_id)
            session.rollback()
            failures.append(exc)
    if failures:
        raise failures[0]


def reconcile(
    session: Session, slack_client: SlackClient, weave_client: WeaveClient, *, feed_channel_id: str | None = None
) -> list[str]:
    """Run every periodic Slack step in order, committing after each so one step's failure doesn't lose the others'
    work. A step that Slack refuses, or that can't reach Weave (the reviewer list comes from Weave's directory), is
    logged and skipped: the next run tries it again. Returns the names of the steps that failed, for the caller to
    count towards the `slack` admin alert."""
    steps = (
        ("sync_reviewer_invites", lambda: sync_reviewer_invites(session, slack_client, weave_client)),
        ("sync_budget_notifications", lambda: sync_budget_notifications(session, slack_client)),
        ("sync_box_notifications", lambda: sync_box_notifications(session, slack_client)),
        ("sync_missed_archives", lambda: sync_missed_archives(session, slack_client)),
        (
            "sync_missed_review_messages",
            lambda: sync_missed_review_messages(session, slack_client, weave_client, feed_channel_id=feed_channel_id),
        ),
    )
    failed = []
    for name, step in steps:
        try:
            step()
        except (SlackError, WeaveUnavailableError):
            logger.exception("krater.slack reconcile step %s failed; continuing", name)
            session.rollback()
            failed.append(name)
        else:
            session.commit()
    return failed


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
    "sync_missed_review_messages",
    "sync_reviewer_invites",
]
