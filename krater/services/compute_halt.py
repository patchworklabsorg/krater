"""Stopping all of Krater Compute at once: the admins' stop switch, and the organization-wide spending caps.

While compute is halted the scheduler starts nothing, searches for nothing, and stops what is running: a batch job's
machine is destroyed and the job goes back in the queue (it resumes from its last checkpoint in storage when compute
is back), and a running box saves /workspace and stops, as for an idle box. Stopped boxes stay stopped, and their
usual destroy time still runs. Members can still queue jobs; they wait, with the reason on the job page.

Two things halt it:

- **The stop switch** (`pause`/`resume`): an admin, with a reason, from `/admin/compute`. It holds until an admin
  turns compute back on. A **regular** stop first has each running job save its checkpoints (and each box
  /workspace), giving up on that after the policy's box sync deadline; an **emergency** stop destroys batch machines
  and stops boxes at once (a box's disk still holds /workspace). An admin can turn a regular stop into an emergency
  one while it's saving.
- **A spending cap** (`ComputeSettings.org_daily_cap_cents`, `org_monthly_cap_cents`; 0 = off, the default): Krater's
  spend across every project since the start of the UTC day or month. It lifts by itself when the period rolls over,
  or when an admin raises the cap. Spend is Krater's own estimate (`krater.compute.spend`), which the hourly
  reconciliation corrects to Vast's charges, so it can trail them by up to an hour. A cap stops as a regular stop
  does: the minutes spent saving cost little next to the work they keep.

The scheduler decides once per pass (`halt_reason`) and records it (`ComputeSettings.halted_since`, `halt_reason`)
so the portal can say why without recomputing spend. When a halt ends, every queued job gets the halted time back on
its max wait, so a long halt doesn't cancel or escalate jobs the moment compute is back (`end_halt`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from krater.models import ComputeJob, ComputeJobState, ComputeSettings, SpendSnapshot
from krater.models.compute_settings import GLOBAL_ID
from krater.services import audit, compute_settings
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed

AUDIT_PAUSE = "compute_paused"
AUDIT_RESUME = "compute_resumed"

#: What members read while an admin has stopped compute. The admin's own reason is for admins only.
PAUSED_REASON = "an admin has stopped Krater Compute for now"

#: The stop switch's modes (`ComputeSettings.paused_mode`).
STOP_REGULAR = "regular"
STOP_EMERGENCY = "emergency"
STOP_MODES = (STOP_REGULAR, STOP_EMERGENCY)


def _row(session: Session) -> ComputeSettings:
    compute_settings.global_policy(session)  # creates the row if it was ever deleted
    return session.get(ComputeSettings, GLOBAL_ID)


def _require_admin_and_reason(actor: Actor, reason: str) -> str:
    if not actor.is_admin:
        raise NotAllowed("Only an admin may stop or start Krater Compute.")
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})
    return reason.strip()


def pause(session: Session, actor: Actor, *, reason: str, now: datetime, mode: str = STOP_REGULAR) -> None:
    """Turn the stop switch on. The scheduler's next pass (within a minute) starts stopping every running machine:
    after saving work for a regular stop, at once for an emergency one. An emergency stop while a regular one holds
    makes it an emergency stop."""
    reason = _require_admin_and_reason(actor, reason)
    if mode not in STOP_MODES:
        raise ValidationFailed({"mode": "Choose a regular or an emergency stop."})
    row = _row(session)
    if row.paused_at is not None:
        if mode == STOP_EMERGENCY and row.paused_mode == STOP_REGULAR:
            row.paused_mode = STOP_EMERGENCY
            audit.record(session, actor, AUDIT_PAUSE, payload={"mode": mode, "escalated": True}, reason=reason)
            session.flush()
            return
        raise InvalidState("Krater Compute is already stopped.")
    row.paused_at, row.paused_reason, row.paused_by_id, row.paused_mode = now, reason, actor.user.id, mode
    audit.record(session, actor, AUDIT_PAUSE, payload={"mode": mode}, reason=reason)
    session.flush()


def resume(session: Session, actor: Actor, *, reason: str, now: datetime) -> None:
    """Turn the stop switch off. Queued jobs start again on the scheduler's next pass, unless a spending cap still
    holds."""
    reason = _require_admin_and_reason(actor, reason)
    row = _row(session)
    if row.paused_at is None:
        raise InvalidState("Krater Compute isn't stopped.")
    stopped_for = now - row.paused_at
    row.paused_at, row.paused_reason, row.paused_by_id, row.paused_mode = None, None, None, None
    audit.record(
        session, actor, AUDIT_RESUME, payload={"stopped_minutes": int(stopped_for.total_seconds() // 60)}, reason=reason
    )
    session.flush()


# -- Spend over a period ------------------------------------------------------------------------------------------


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


def _totals_at(session: Session, at: datetime | None) -> dict[uuid.UUID, int]:
    """Each project's cumulative spend as of `at` (its latest snapshot then), or now for `None`."""
    stmt = (
        sa.select(SpendSnapshot.project_id, SpendSnapshot.estimated_spend_cents)
        .ext(distinct_on(SpendSnapshot.project_id))
        .order_by(SpendSnapshot.project_id, SpendSnapshot.taken_at.desc())
    )
    if at is not None:
        stmt = stmt.where(SpendSnapshot.taken_at <= at)
    return {project_id: int(cents) for project_id, cents in session.execute(stmt)}


def spend_since(session: Session, since: datetime | None) -> dict[uuid.UUID, int]:
    """Each project's spend since `since` (all time for `None`), in cents: its total now less its total then. A
    project whose total fell (the reconciliation lowered it to Vast's charges) counts as 0, never less."""
    now_totals = _totals_at(session, None)
    if since is None:
        return now_totals
    then = _totals_at(session, since)
    return {project_id: max(0, total - then.get(project_id, 0)) for project_id, total in now_totals.items()}


@dataclass(frozen=True)
class CapStatus:
    label: str  # "daily", "monthly"
    cap_cents: int  # 0 = off
    spent_cents: int
    resets_at: datetime

    @property
    def reached(self) -> bool:
        return self.cap_cents > 0 and self.spent_cents >= self.cap_cents


def cap_statuses(session: Session, now: datetime) -> list[CapStatus]:
    limits = compute_settings.org_limits(session)
    today = day_start(now)
    month = month_start(now)
    next_month = (month + timedelta(days=32)).replace(day=1)
    statuses = []
    for label, cap, since, resets in (
        ("daily", limits.org_daily_cap_cents, today, today + timedelta(days=1)),
        ("monthly", limits.org_monthly_cap_cents, month, next_month),
    ):
        spent = sum(spend_since(session, since).values()) if cap > 0 else 0
        statuses.append(CapStatus(label, cap, spent, resets))
    return statuses


def halt_reason(session: Session, now: datetime) -> str | None:
    """Why compute is stopped right now, as members read it, or `None` while it runs. The stop switch first, then
    the spending caps."""
    row = _row(session)
    if row.paused_at is not None:
        return PAUSED_REASON
    for status in cap_statuses(session, now):
        if status.reached:
            when = (
                f"{status.resets_at:%H:%M} UTC"
                if status.label == "daily"
                else f"{status.resets_at:%B} {status.resets_at.day} (UTC)"
            )
            return (
                f"Krater has reached its {status.label} spending cap across all projects; compute starts again {when} "
                "or when an admin raises the cap"
            )
    return None


def saves_first(session: Session) -> bool:
    """Whether the halt in force lets running machines save their work before they stop: every halt but an
    emergency stop (or a stop from before stops had modes)."""
    row = _row(session)
    return row.paused_at is None or row.paused_mode == STOP_REGULAR


def record(session: Session, reason: str | None, *, now: datetime) -> bool:
    """Store this pass's decision. When a halt ends, give every queued job the halted time back on its max wait.
    Returns whether compute is halted. The caller commits."""
    row = _row(session)
    if reason is not None:
        if row.halted_since is None:
            row.halted_since = now
        row.halt_reason = reason
        session.flush()
        return True
    if row.halted_since is not None:
        end_halt(session, row.halted_since, now=now)
        row.halted_since, row.halt_reason = None, None
        session.flush()
    return False


def end_halt(session: Session, since: datetime, *, now: datetime) -> None:
    """Move each queued job's `queued_at` forward by the halt's length, keeping their order: the max wait counts
    from it, and a job shouldn't be cancelled or moved on demand for time compute was stopped."""
    gap = now - since
    if gap <= timedelta(0):
        return
    session.execute(
        sa.update(ComputeJob)
        .where(ComputeJob.state == ComputeJobState.QUEUED, ComputeJob.queued_at < now)
        .values(queued_at=sa.func.least(ComputeJob.queued_at + gap, now))
    )


def current(session: Session) -> str | None:
    """Why compute is stopped, as of the scheduler's last pass or the switch itself, for the portal (a box's resume,
    the job page). `None` while it runs."""
    row = _row(session)
    if row.paused_at is not None:
        return PAUSED_REASON
    return row.halt_reason


def require_running(session: Session) -> None:
    """Refuse something that would start billing (resuming a box) while compute is stopped."""
    reason = current(session)
    if reason is not None:
        raise InvalidState(f"Not now: {reason}.")


__all__ = [
    "AUDIT_PAUSE",
    "AUDIT_RESUME",
    "PAUSED_REASON",
    "STOP_EMERGENCY",
    "STOP_MODES",
    "STOP_REGULAR",
    "CapStatus",
    "cap_statuses",
    "current",
    "day_start",
    "end_halt",
    "halt_reason",
    "month_start",
    "pause",
    "record",
    "saves_first",
    "require_running",
    "resume",
    "spend_since",
]
