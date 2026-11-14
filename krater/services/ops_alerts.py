"""Telling admins when something behind the portal keeps failing: a Slack message in the alerts channel
(`KRATER_SLACK_ALERTS_CHANNEL_ID`), another while it goes on, and one when it clears.

The worker's tasks catch their errors and log them, so before this a broken Quilt delivery, Slack app or compute
scheduler went unseen until someone read the logs. Two kinds of problem open an alert (`OpsAlert`, one row per key):

- **A task that keeps failing**: the worker counts each failure (`record_failure`). An alert opens once a task has
  failed `threshold` times with no gap longer than its `window`, and clears after a whole window without a failure.
- **A condition** the periodic `ops_watch` task checks (`check`): Quilt's outbox backing up, prices not refreshed,
  background jobs ending in error, compute stopped (by the switch or a spending cap), charges for machines Krater
  never rented, the provider's credit running low or out, storage nearly full. It opens while the condition holds and
  clears when it doesn't.

`deliver` posts what changed. A dead worker can't report itself: `/healthz/worker` answers 503 when its heartbeat is
stale, for an uptime check outside Krater to watch (`docs/dev/production.md`).

Framework-free; `krater.worker.app` calls it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import (
    AuditEvent,
    ComputeSettings,
    GpuPrice,
    OpsAlert,
    QuiltOutbox,
    QuiltOutboxState,
    StashArchive,
    StashArchiveState,
    WorkerHeartbeat,
)
from krater.slack import SlackClient, SlackError

logger = logging.getLogger(__name__)

#: An open alert is posted again this often while it lasts.
RENOTIFY = timedelta(hours=6)
#: The worker counts as down when its heartbeat (every minute) is older than this.
HEARTBEAT_STALE = timedelta(minutes=5)
#: Quilt's outbox is backed up when Krater has kept trying to send an event this long.
QUILT_BACKLOG_AGE = timedelta(minutes=30)
#: The price catalog is refreshed daily; past this it's stale.
PRICES_STALE = timedelta(hours=48)
#: Background jobs that ended in an error (an exception a task didn't catch) within this window open an alert.
FAILED_JOBS_WINDOW = timedelta(hours=1)
#: A due background job waiting longer than this is stuck: a healthy worker starts one within seconds, so it's
#: behind a lock some other job never let go of (a hung task, or one whose worker died), or the worker isn't taking
#: jobs. The compute scheduler's lock stuck like this stops all of Krater Compute without anything failing.
JOBS_WAITING_AGE = timedelta(minutes=15)
#: Charges for machines Krater never rented (`krater.compute.reconcile`) keep their alert open this long.
UNKNOWN_CHARGES_WINDOW = timedelta(hours=24)
#: The audit action the reconciliation writes for them (`krater.compute.reconcile.AUDIT_UNKNOWN_CHARGES`).
AUDIT_UNKNOWN_CHARGES = "compute_unknown_charges"


@dataclass(frozen=True)
class FailureRule:
    title: str
    threshold: int
    window: timedelta


#: The tasks whose failures the worker reports, by key.
FAILURE_RULES = {
    "compute_schedule": FailureRule("The compute scheduler keeps failing", 5, timedelta(minutes=15)),
    "compute_reconcile": FailureRule("Spend reconciliation keeps failing", 2, timedelta(hours=3)),
    "compute_storage_cleanup": FailureRule("Cleaning up unfinished uploads keeps failing", 2, timedelta(hours=3)),
    "price_history": FailureRule("Recording Vast's prices keeps failing", 3, timedelta(hours=4)),
    "grant_release": FailureRule("Releasing held budget grants keeps failing", 3, timedelta(hours=1)),
    # Admins' keys leave every box an hour after the last good sync (`box_access.ROSTER_STALE`), so alert well before.
    "box_admins": FailureRule("Syncing admins' box SSH access from Weave keeps failing", 3, timedelta(minutes=30)),
    # Daily, so two failures in a row.
    "ospool_accounting": FailureRule("Importing OSPool usage from OSG keeps failing", 2, timedelta(hours=49)),
    "quilt": FailureRule("Sending events to Quilt keeps failing", 5, timedelta(minutes=15)),
    "stash": FailureRule("Archiving submission links in Stash keeps failing", 5, timedelta(minutes=15)),
    "slack": FailureRule("Slack tasks keep failing", 3, timedelta(minutes=30)),
    # The worker's `stalled_jobs_sweep`: one is enough, since a job cut off mid-run may have left work half done.
    "stalled_jobs": FailureRule("A worker stopped in the middle of a background job", 1, timedelta(hours=1)),
}
#: The conditions `check` watches, by key.
CONDITION_TITLES = {
    "quilt_backlog": "Quilt's outbox is backing up",
    "stash_backlog": "Archive requests to Stash are backing up",
    "prices_stale": "GPU prices aren't being refreshed",
    "failed_jobs": "Background jobs are ending in errors",
    "jobs_waiting": "Background jobs are stuck waiting",
    "compute_halted": "Krater Compute is stopped",
    "unknown_charges": "Vast charged for machines Krater never rented",
    "low_credit": "The Vast account's credit is low",
    "credit_out": "The Vast account is out of credit",
    "store_full": "Krater's storage is nearly full",
}


def _row(session: Session, key: str) -> OpsAlert:
    row = session.get(OpsAlert, key, with_for_update=True)
    if row is None:
        row = OpsAlert(key=key, failures=0, message="")
        session.add(row)
    return row


def record_failure(session: Session, key: str, message: str, *, now: datetime) -> None:
    """Count one failure of the task `key` (one of `FAILURE_RULES`). The caller commits."""
    rule = FAILURE_RULES[key]
    row = _row(session, key)
    if row.last_failed_at is None or now - row.last_failed_at > rule.window:
        row.failures, row.first_failed_at = 0, now
    row.failures += 1
    row.last_failed_at = now
    row.message = message[:2000]
    if row.failures >= rule.threshold and row.opened_at is None:
        row.opened_at = now
    session.flush()


def _set_condition(session: Session, key: str, message: str | None, *, now: datetime) -> None:
    """Open `key` with `message` while the condition holds; `None` clears it."""
    row = session.get(OpsAlert, key, with_for_update=True)
    if message is None:
        if row is not None and row.opened_at is not None:
            row.opened_at = None
        return
    row = row or _row(session, key)
    row.message = message
    row.last_failed_at = now
    if row.opened_at is None:
        row.opened_at, row.first_failed_at = now, now


def _stash_backlog(session: Session, settings: Settings, now: datetime) -> str | None:
    if not settings.stash_url:
        return None  # not archiving yet: the rows keep every link until it's set
    # As `_quilt_backlog`: measured from the first attempt. A run that never gets to try opens the "stash" task alert.
    oldest = session.scalar(
        sa.select(sa.func.min(StashArchive.first_attempted_at)).where(StashArchive.state == StashArchiveState.PENDING)
    )
    if oldest is None or now - oldest < QUILT_BACKLOG_AGE:
        return None
    return f"Krater has been trying to archive a submitted link in Stash since {oldest:%Y-%m-%d %H:%M} UTC."


def _quilt_backlog(session: Session, settings: Settings, now: datetime) -> str | None:
    if not settings.quilt_url:
        return None  # not sending yet: the outbox keeps everything until it's set
    failed = session.scalar(
        sa.select(sa.func.count()).select_from(QuiltOutbox).where(QuiltOutbox.state == QuiltOutboxState.FAILED)
    )
    # Measured from the first attempt, not `occurred_at`: events queued before sending was switched on are old the
    # moment it is, and go out within minutes. One Krater has kept trying for half an hour is stuck. A run that
    # never gets to try (no token, Quilt refusing every request) opens the "quilt" task alert instead.
    oldest = session.scalar(
        sa.select(sa.func.min(QuiltOutbox.first_attempted_at)).where(QuiltOutbox.state == QuiltOutboxState.PENDING)
    )
    waiting = oldest is not None and now - oldest >= QUILT_BACKLOG_AGE
    if not failed and not waiting:
        return None
    parts = []
    if failed:
        parts.append(f"{failed} event{'s' if failed != 1 else ''} Quilt refused for good (an admin must look)")
    if waiting:
        parts.append(f"Krater has been trying to send an event since {oldest:%Y-%m-%d %H:%M} UTC")
    return "; ".join(parts) + "."


def _prices_stale(session: Session, now: datetime) -> str | None:
    newest = session.scalar(sa.select(sa.func.max(GpuPrice.refreshed_at)))
    if newest is not None and now - newest < PRICES_STALE:
        return None
    since = f"since {newest:%Y-%m-%d %H:%M} UTC" if newest is not None else "ever"
    return f"The price catalog behind /pricing and the budget estimator hasn't refreshed {since}."


def _failed_jobs(session: Session, now: datetime) -> str | None:
    rows = session.execute(
        sa.text(
            "SELECT j.task_name, count(*) FROM procrastinate_events e "
            "JOIN procrastinate_jobs j ON j.id = e.job_id "
            "WHERE e.type = 'failed' AND e.at >= :since GROUP BY j.task_name ORDER BY j.task_name"
        ),
        {"since": now - FAILED_JOBS_WINDOW},
    ).all()
    if not rows:
        return None
    return "In the last hour: " + ", ".join(f"{name} ×{count}" for name, count in rows) + ". See the worker's log."


def _jobs_waiting(session: Session, now: datetime) -> str | None:
    """The tasks with a job that has been due for over `JOBS_WAITING_AGE` and still hasn't started, oldest first."""
    rows = session.execute(
        sa.text(
            "SELECT j.task_name, min(greatest(e.at, coalesce(j.scheduled_at, e.at))) AS due "
            "FROM procrastinate_jobs j JOIN procrastinate_events e ON e.job_id = j.id AND e.type = 'deferred' "
            "WHERE j.status = 'todo' GROUP BY j.task_name "
            "HAVING min(greatest(e.at, coalesce(j.scheduled_at, e.at))) < :cutoff ORDER BY due"
        ),
        {"cutoff": now - JOBS_WAITING_AGE},
    ).all()
    if not rows:
        return None
    waiting = ", ".join(f"{name} since {due:%Y-%m-%d %H:%M}" for name, due in rows)
    return (
        f"Waiting to start: {waiting} UTC. A job holding their lock may have hung, or the worker isn't taking jobs. "
        "See the worker's log and the runbook's \"Stuck background jobs\"."
    )


def _compute_halted(session: Session) -> str | None:
    row = session.get(ComputeSettings, 1)
    if row is None:
        return None
    if row.paused_at is not None:
        return (
            f"An admin stopped all compute at {row.paused_at:%Y-%m-%d %H:%M} UTC: {row.paused_reason}. Turn it back on "
            "from /admin/compute."
        )
    if row.halt_reason is not None:
        return f"{row.halt_reason[:1].upper()}{row.halt_reason[1:]}. See /admin/compute/overview."
    return None


def _unknown_charges(session: Session, now: datetime) -> str | None:
    events = session.scalars(
        sa.select(AuditEvent).where(
            AuditEvent.action == AUDIT_UNKNOWN_CHARGES, AuditEvent.created_at >= now - UNKNOWN_CHARGES_WINDOW
        )
    ).all()
    sources = sorted({source for event in events for source in (event.payload or {}).get("sources", [])})
    if not sources:
        return None
    return (
        f"Vast billed {', '.join(sources[:10])}{' and more' if len(sources) > 10 else ''}, labelled as Krater's, "
        "which Krater has no record of renting. The scheduler destroys any it still sees; check the Vast account for "
        "a leaked key or someone renting by hand. See the runbook on /admin/compute."
    )


def _low_credit(session: Session) -> str | None:
    from krater.services import compute_settings, compute_watch

    threshold = compute_settings.org_limits(session).credit_alert_cents
    latest = compute_watch.latest_credit(session)
    if threshold <= 0 or latest is None or latest.credit_microusd >= threshold * 10_000:
        return None
    if latest.credit_microusd <= 0:
        return None  # `credit_out` says so
    return (
        f"${latest.credit_microusd / 1_000_000:,.2f} left as of {latest.observed_at:%Y-%m-%d %H:%M} UTC, under the "
        f"${threshold / 100:,.2f} alert. Vast stops every machine when the credit runs out. Add credit on Vast."
    )


def _credit_out(session: Session) -> str | None:
    """The account has no credit left: Vast has stopped every machine and rents nothing. Always on, with or without
    a low credit alert."""
    from krater.services import compute_watch

    latest = compute_watch.latest_credit(session)
    if latest is None or latest.credit_microusd > 0:
        return None
    return (
        f"${latest.credit_microusd / 1_000_000:,.2f} left as of {latest.observed_at:%Y-%m-%d %H:%M} UTC. Vast has "
        "stopped every machine and rents nothing. Krater keeps the stopped machines, with their disks, starts them "
        "again once there is credit, and holds the queue until then. Add credit on Vast."
    )


def _store_full(session: Session, settings: Settings) -> str | None:
    from krater.services import compute_watch

    capacity = settings.compute_store_capacity_gb * 1024**3
    if capacity <= 0:
        return None
    used, _, observed = compute_watch.storage_totals(session)
    if observed is None or used * 100 < capacity * compute_watch.STORE_ALERT_PERCENT:
        return None
    return (
        f"{used / 1024**3:,.1f} GB stored of {settings.compute_store_capacity_gb} GB, as of {observed:%H:%M} UTC. "
        "Uploads fail for every project once it's full. See which projects use most on /admin/compute/overview."
    )


def check(session: Session, settings: Settings, *, now: datetime) -> None:
    """Open or clear every condition, and clear task alerts that have gone a whole window without a failure. The
    caller commits."""
    _set_condition(session, "quilt_backlog", _quilt_backlog(session, settings, now), now=now)
    _set_condition(session, "stash_backlog", _stash_backlog(session, settings, now), now=now)
    _set_condition(session, "prices_stale", _prices_stale(session, now), now=now)
    _set_condition(session, "failed_jobs", _failed_jobs(session, now), now=now)
    _set_condition(session, "jobs_waiting", _jobs_waiting(session, now), now=now)
    _set_condition(session, "compute_halted", _compute_halted(session), now=now)
    _set_condition(session, "unknown_charges", _unknown_charges(session, now), now=now)
    _set_condition(session, "low_credit", _low_credit(session), now=now)
    _set_condition(session, "credit_out", _credit_out(session), now=now)
    _set_condition(session, "store_full", _store_full(session, settings), now=now)
    for key, rule in FAILURE_RULES.items():
        row = session.get(OpsAlert, key, with_for_update=True)
        if row is not None and row.last_failed_at is not None and now - row.last_failed_at > rule.window:
            row.failures = 0
            row.opened_at = None
    session.flush()


#: How far back `recent` lists problems that never reached their alert's threshold or have cleared.
RECENT_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class RecentProblem:
    key: str
    title: str
    open: bool
    failures: int
    first_failed_at: datetime | None
    last_failed_at: datetime | None
    message: str


def recent(session: Session, *, now: datetime) -> list[RecentProblem]:
    """Every alert that is open, and every problem seen in the last `RECENT_WINDOW`, newest first, for the admin
    compute page: Slack is otherwise the only place admins see them, and a failure under its alert's threshold
    (four failed scheduler passes, say) is posted nowhere."""
    rows = session.scalars(
        sa.select(OpsAlert)
        .where(sa.or_(OpsAlert.opened_at.is_not(None), OpsAlert.last_failed_at >= now - RECENT_WINDOW))
        .order_by(OpsAlert.last_failed_at.desc().nulls_last())
    )
    return [
        RecentProblem(
            key=row.key,
            title=_title(row.key),
            open=row.opened_at is not None,
            failures=row.failures,
            first_failed_at=row.first_failed_at,
            last_failed_at=row.last_failed_at,
            message=row.message,
        )
        for row in rows
    ]


def _title(key: str) -> str:
    rule = FAILURE_RULES.get(key)
    return rule.title if rule is not None else CONDITION_TITLES.get(key, key)


def _open_text(row: OpsAlert, *, again: bool) -> str:
    title = _title(row.key)
    lines = [f":rotating_light: *Krater: {title}*" + (" (still)" if again else "")]
    if row.key in FAILURE_RULES:
        lines.append(f"{row.failures} failures since {row.first_failed_at:%Y-%m-%d %H:%M} UTC. The last one:")
    if row.message:
        lines.append(f"> {row.message}")
    return "\n".join(lines)


def deliver(session: Session, slack_client: SlackClient | None, channel_id: str | None, *, now: datetime) -> list[str]:
    """Post each alert that opened, is due a reminder or cleared, and return their keys. Without a channel the
    alerts go to the log instead. A Slack error leaves them for the next run. The caller commits."""
    rows = session.scalars(
        sa.select(OpsAlert)
        .where(
            sa.or_(
                sa.and_(
                    OpsAlert.opened_at.is_not(None),
                    sa.or_(OpsAlert.notified_at.is_(None), OpsAlert.notified_at <= now - RENOTIFY),
                ),
                sa.and_(OpsAlert.opened_at.is_(None), OpsAlert.notified_at.is_not(None)),
            )
        )
        .order_by(OpsAlert.key)
        .with_for_update()
    ).all()
    posted = []
    for row in rows:
        cleared = row.opened_at is None
        if cleared:
            text = f":white_check_mark: *Krater: cleared:* {_title(row.key)}"
        else:
            text = _open_text(row, again=row.notified_at is not None)
        if slack_client is None or not channel_id:
            (logger.info if cleared else logger.error)("krater ops alert: %s", text)
        else:
            try:
                slack_client.post_message(
                    channel_id, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text=text
                )
            except SlackError:
                logger.exception("krater: could not post the ops alert %s; trying again next run", row.key)
                continue
        row.notified_at = None if cleared else now
        posted.append(row.key)
    session.flush()
    return posted


def beat(session: Session, *, now: datetime) -> None:
    """Record the worker's heartbeat. The caller commits."""
    row = session.get(WorkerHeartbeat, 1)
    if row is None:
        session.add(WorkerHeartbeat(id=1, at=now))
    else:
        row.at = max(row.at, now)
    session.flush()


def worker_alive(session: Session, *, now: datetime) -> tuple[bool, datetime | None]:
    """Whether the worker's heartbeat is fresh, and when it last beat."""
    row = session.get(WorkerHeartbeat, 1)
    if row is None:
        return False, None
    return now - row.at <= HEARTBEAT_STALE, row.at
