"""Telling admins when something behind the portal keeps failing: a Slack message in the alerts channel
(`KRATER_SLACK_ALERTS_CHANNEL_ID`), another while it goes on, and one when it clears.

The worker's tasks catch their errors and log them, so before this a broken Quilt delivery, Slack app or compute
scheduler went unseen until someone read the logs. Two kinds of problem open an alert (`OpsAlert`, one row per key):

- **A task that keeps failing**: the worker counts each failure (`record_failure`). An alert opens once a task has
  failed `threshold` times with no gap longer than its `window`, and clears after a whole window without a failure.
- **A condition** the periodic `ops_watch` task checks (`check`): Quilt's outbox backing up, prices not refreshed,
  background jobs ending in error. It opens while the condition holds and clears when it doesn't.

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
from krater.models import GpuPrice, OpsAlert, QuiltOutbox, QuiltOutboxState, WorkerHeartbeat
from krater.slack import SlackClient, SlackError

logger = logging.getLogger(__name__)

#: An open alert is posted again this often while it lasts.
RENOTIFY = timedelta(hours=6)
#: The worker counts as down when its heartbeat (every minute) is older than this.
HEARTBEAT_STALE = timedelta(minutes=5)
#: Quilt's outbox is backed up when an event has waited this long to be sent.
QUILT_BACKLOG_AGE = timedelta(minutes=30)
#: The price catalog is refreshed daily; past this it's stale.
PRICES_STALE = timedelta(hours=48)
#: Background jobs that ended in an error (an exception a task didn't catch) within this window open an alert.
FAILED_JOBS_WINDOW = timedelta(hours=1)


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
    "quilt": FailureRule("Sending events to Quilt keeps failing", 5, timedelta(minutes=15)),
    "slack": FailureRule("Slack tasks keep failing", 3, timedelta(minutes=30)),
}
#: The conditions `check` watches, by key.
CONDITION_TITLES = {
    "quilt_backlog": "Quilt's outbox is backing up",
    "prices_stale": "GPU prices aren't being refreshed",
    "failed_jobs": "Background jobs are ending in errors",
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


def _quilt_backlog(session: Session, settings: Settings, now: datetime) -> str | None:
    if not settings.quilt_url:
        return None  # not sending yet: the outbox keeps everything until it's set
    failed = session.scalar(
        sa.select(sa.func.count()).select_from(QuiltOutbox).where(QuiltOutbox.state == QuiltOutboxState.FAILED)
    )
    oldest = session.scalar(
        sa.select(sa.func.min(QuiltOutbox.occurred_at)).where(QuiltOutbox.state == QuiltOutboxState.PENDING)
    )
    waiting = oldest is not None and now - oldest >= QUILT_BACKLOG_AGE
    if not failed and not waiting:
        return None
    parts = []
    if failed:
        parts.append(f"{failed} event{'s' if failed != 1 else ''} Quilt refused for good (an admin must look)")
    if waiting:
        parts.append(f"the oldest unsent event has waited since {oldest:%Y-%m-%d %H:%M} UTC")
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


def check(session: Session, settings: Settings, *, now: datetime) -> None:
    """Open or clear every condition, and clear task alerts that have gone a whole window without a failure. The
    caller commits."""
    _set_condition(session, "quilt_backlog", _quilt_backlog(session, settings, now), now=now)
    _set_condition(session, "prices_stale", _prices_stale(session, now), now=now)
    _set_condition(session, "failed_jobs", _failed_jobs(session, now), now=now)
    for key, rule in FAILURE_RULES.items():
        row = session.get(OpsAlert, key, with_for_update=True)
        if row is not None and row.last_failed_at is not None and now - row.last_failed_at > rule.window:
            row.failures = 0
            row.opened_at = None
    session.flush()


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
