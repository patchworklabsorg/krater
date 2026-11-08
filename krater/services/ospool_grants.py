"""OSPool grants: projects OSPool funds outside Krater, reported from OSG's accounting (docs/SPEC.md "OSPool grants").

A small grant can run on the OSPool instead of on Krater's dollars: the team gets its own OSG accounts and runs
under an OSPool project name Patchwork registered for that grant (`PatchworkLabs_Ganymede01`, say). Krater doesn't
run those jobs. It records the grant and reports what the team used:

- **Recording.** An admin records the grant on an approved project with the OSPool project name, the day usage
  counts from, and the GPU to value it as. Only verified US residents may use OSPool capacity, so every team member
  must be one (`residency.require_us_residents`, asked fresh; Weave down refuses). The project must hold no Krater
  dollars (reclaim them first). Its pending budget hold, if any, is cancelled and its funding hold cleared: the
  OSPool funds it instead, so nothing reaches the ledger or Quilt. An admin can't record one on a project they lead
  or are on.
- **Usage.** A daily worker task (`import_all`) reads each grant's usage per UTC day from OSG's public accounting
  (`krater.ospool.accounting`). OSG corrects recent days, so it reads the last `REIMPORT_DAYS` again and replaces
  their hours.
- **Value.** Each day is valued at what renting the GPU on demand would have cost: the admin's price (a neocloud's
  posted price, say) when set, else Krater's own (`pricing.on_demand_rate`). A day keeps the price it was first
  valued at, so a later price change moves only later days. A day with no price gets one once a price exists.
- **Reporting only.** No cap and no alerts: OSG enforces nothing per grant, and Krater stops nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import OspoolGrant, OspoolUsageDay, Project
from krater.ospool.accounting import AccountingError, DayUsage, FakeAccounting, LiveAccounting
from krater.services import audit, budget, grants, pricing, residency, teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.services.funding_holds import HOLDABLE_STATUSES
from krater.services.projects import _OWN_PROJECT_MESSAGE, _lock_project
from krater.weave import WeaveClient

logger = logging.getLogger(__name__)

AUDIT_RECORD = "admin_record_ospool_grant"
AUDIT_VALUATION = "admin_change_ospool_valuation"
#: `OspoolUsageDay.rate_source` for the grant's own price.
SOURCE_ADMIN = "admin"
#: How many recent days each import reads again, since OSG corrects them.
REIMPORT_DAYS = 35
#: How a registered OSPool project name looks (`PatchworkLabs_Ganymede01`).
NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,127}")
_HOURS = Decimal("0.0001")


AccountingClient = LiveAccounting | FakeAccounting


def for_project(session: Session, project: Project) -> OspoolGrant | None:
    return session.scalar(sa.select(OspoolGrant).where(OspoolGrant.project_id == project.id))


def project_ids_with_grants(session: Session, project_ids: list) -> set:
    """Which of `project_ids` have an OSPool grant, in one query (for badges on lists)."""
    if not project_ids:
        return set()
    return set(session.scalars(sa.select(OspoolGrant.project_id).where(OspoolGrant.project_id.in_(project_ids))))


def all_grants(session: Session) -> list[OspoolGrant]:
    return list(session.scalars(sa.select(OspoolGrant).order_by(OspoolGrant.created_at)))


def _validate(
    session: Session,
    *,
    gpu_name: str,
    rate_override_microusd: int | None,
    rate_note: str,
    reason: str,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not (reason and reason.strip()):
        errors["reason"] = "A reason is required."
    if not gpu_name:
        errors["gpu_name"] = "Choose the GPU to value the usage as."
    elif len(gpu_name) > 64:
        errors["gpu_name"] = "That GPU name is too long."
    elif rate_override_microusd is None and gpu_name not in pricing.list_accelerator_names(session):
        errors["gpu_name"] = "Krater has no price for that GPU. Choose one it prices, or give a price per GPU-hour."
    if rate_override_microusd is not None and rate_override_microusd <= 0:
        errors["rate"] = "Enter a price above zero, or leave it blank."
    if len(rate_note) > 200:
        errors["rate_note"] = "Keep the note under 200 characters."
    if rate_override_microusd is not None and not rate_note.strip():
        errors["rate_note"] = "Say where the price comes from (a neocloud's posted price, say)."
    return errors


def record_grant(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    ospool_project_name: str,
    counted_from: date,
    gpu_name: str,
    rate_override_microusd: int | None,
    rate_note: str,
    reason: str,
    weave_client: WeaveClient,
    now: datetime,
) -> OspoolGrant:
    """Admin: record that the OSPool funds `project` under `ospool_project_name`. The caller commits, then posts the
    change in the project's channel."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may record an OSPool grant.")
    if teams.has_conflict(project, actor.user.id):
        raise NotAllowed(_OWN_PROJECT_MESSAGE)
    name = ospool_project_name.strip()
    gpu_name = gpu_name.strip()
    rate_note = rate_note.strip()
    errors = _validate(
        session, gpu_name=gpu_name, rate_override_microusd=rate_override_microusd, rate_note=rate_note, reason=reason
    )
    if not NAME_PATTERN.fullmatch(name):
        errors["ospool_project_name"] = (
            "Enter the OSPool project name as OSG registered it, like PatchworkLabs_Ganymede01."
        )
    elif session.scalar(sa.select(OspoolGrant.id).where(OspoolGrant.ospool_project_name == name)) is not None:
        errors["ospool_project_name"] = "Another project's grant already uses that OSPool project name."
    if counted_from > now.date():
        errors["counted_from"] = "Usage can't count from a day that hasn't come yet."
    if errors:
        raise ValidationFailed(errors)

    _lock_project(session, project)
    if project.status not in HOLDABLE_STATUSES:
        raise InvalidState("An OSPool grant can only be recorded on an approved project.")
    if for_project(session, project) is not None:
        raise InvalidState("This project already has an OSPool grant.")
    if budget.ceiling_cents(session, project) > 0:
        raise InvalidState(
            "This project already holds Krater budget. Reclaim it first: an OSPool grant funds the project instead."
        )
    residency.require_us_residents(session, weave_client, project)

    cancelled = grants.cancel_pending(session, project, actor=actor, reason="Funded by an OSPool grant instead.")
    # The OSPool is what the project was waiting on, if anything. Clearing the flag releases nothing: the hold above
    # is cancelled, and the ledger stays empty.
    previous_hold = project.funding_hold
    project.funding_hold = None
    grant = OspoolGrant(
        project_id=project.id,
        ospool_project_name=name,
        counted_from=counted_from,
        gpu_name=gpu_name,
        rate_override_microusd=rate_override_microusd,
        rate_note=rate_note,
        actor_id=actor.user.id,
        reason=reason.strip(),
    )
    session.add(grant)
    session.flush()
    audit.record(
        session,
        actor,
        AUDIT_RECORD,
        project=project,
        payload={
            "ospool_project_name": name,
            "counted_from": counted_from.isoformat(),
            "gpu_name": gpu_name,
            "rate_override_microusd": rate_override_microusd,
            "rate_note": rate_note,
            "cancelled_hold_cents": cancelled.amount_cents if cancelled is not None else None,
            "cleared_funding_hold": previous_hold.value if previous_hold is not None else None,
        },
        reason=reason.strip(),
    )
    return grant


def update_valuation(
    session: Session,
    actor: Actor,
    *,
    grant: OspoolGrant,
    gpu_name: str,
    rate_override_microusd: int | None,
    rate_note: str,
    reason: str,
) -> None:
    """Admin: change the GPU and price a grant's usage is valued at. Days already valued keep their price."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may change an OSPool grant's valuation.")
    if teams.has_conflict(grant.project, actor.user.id):
        raise NotAllowed(_OWN_PROJECT_MESSAGE)
    gpu_name = gpu_name.strip()
    rate_note = rate_note.strip()
    errors = _validate(
        session, gpu_name=gpu_name, rate_override_microusd=rate_override_microusd, rate_note=rate_note, reason=reason
    )
    if errors:
        raise ValidationFailed(errors)
    unchanged = (
        gpu_name == grant.gpu_name
        and rate_override_microusd == grant.rate_override_microusd
        and rate_note == grant.rate_note
    )
    if unchanged:
        raise InvalidState("That's the valuation the grant already has.")
    previous = {
        "gpu_name": grant.gpu_name,
        "rate_override_microusd": grant.rate_override_microusd,
        "rate_note": grant.rate_note,
    }
    grant.gpu_name = gpu_name
    grant.rate_override_microusd = rate_override_microusd
    grant.rate_note = rate_note
    audit.record(
        session,
        actor,
        AUDIT_VALUATION,
        project=grant.project,
        payload={
            "gpu_name": gpu_name,
            "rate_override_microusd": rate_override_microusd,
            "rate_note": rate_note,
            "previous": previous,
        },
        reason=reason.strip(),
    )
    session.flush()


# --------------------------------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------------------------------


def _current_rate(session: Session, grant: OspoolGrant, now: datetime) -> tuple[int, str] | None:
    """The price per GPU-hour, in micro-dollars, that values a day first valued now, and where it came from."""
    if grant.rate_override_microusd is not None:
        return grant.rate_override_microusd, SOURCE_ADMIN
    rate = pricing.on_demand_rate(session, accelerator_name=grant.gpu_name, now=now)
    if rate is None:
        return None
    rate_cents, source = rate
    return rate_cents * 10_000, source


def value_cents(gpu_hours: Decimal, rate_microusd: int) -> int:
    """`gpu_hours` at `rate_microusd` per hour, in whole cents (half a cent rounds up)."""
    return int((gpu_hours * rate_microusd / 10_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _apply(row: OspoolUsageDay, usage: DayUsage | None, rate: tuple[int, str] | None, now: datetime) -> None:
    row.gpu_hours = usage.gpu_hours.quantize(_HOURS) if usage else Decimal(0)
    row.core_hours = usage.core_hours.quantize(_HOURS) if usage else Decimal(0)
    row.jobs = usage.jobs if usage else 0
    if row.rate_microusd is None and rate is not None:
        row.rate_microusd, row.rate_source = rate
    row.value_cents = value_cents(row.gpu_hours, row.rate_microusd) if row.rate_microusd is not None else None
    row.imported_at = now


def import_usage(session: Session, grant: OspoolGrant, client: AccountingClient, *, now: datetime) -> int:
    """Read `grant`'s usage from OSG's accounting and store it per day. The first import reads from
    `counted_from`; later ones read the last `REIMPORT_DAYS` again. A day OSG no longer reports is set to zero.
    Raises `AccountingError` (after recording it on the grant) when OSG can't be read. Returns the days stored.
    Flushes, doesn't commit."""
    today = now.date()
    existing = {row.day: row for row in grant.usage_days}
    start = grant.counted_from if not existing else max(grant.counted_from, today - timedelta(days=REIMPORT_DAYS))
    end = today + timedelta(days=1)
    try:
        days = client.daily_usage(grant.ospool_project_name, start, end)
    except AccountingError as exc:
        grant.last_import_error = str(exc)[:500]
        session.flush()
        raise
    reported = {usage.day: usage for usage in days if start <= usage.day < end}
    rate = _current_rate(session, grant, now)
    for day in sorted(set(reported) | {day for day in existing if start <= day < end}):
        row = existing.get(day)
        if row is None:
            row = OspoolUsageDay(grant=grant, day=day)
            session.add(row)
        _apply(row, reported.get(day), rate, now)
    # A day valued before Krater had a price gets one now, outside the window too.
    if rate is not None:
        for row in existing.values():
            if row.rate_microusd is None and not (start <= row.day < end):
                row.rate_microusd, row.rate_source = rate
                row.value_cents = value_cents(row.gpu_hours, row.rate_microusd)
    grant.last_imported_at = now
    grant.last_import_error = None
    session.flush()
    return len(reported)


def import_all(session: Session, client: AccountingClient, *, now: datetime) -> list[str]:
    """Import every grant's usage, committing after each so one failure keeps the others. Returns the OSPool project
    names that couldn't be read."""
    failed: list[str] = []
    for grant_id in list(session.scalars(sa.select(OspoolGrant.id).order_by(OspoolGrant.created_at))):
        grant = session.get(OspoolGrant, grant_id)
        try:
            import_usage(session, grant, client, now=now)
        except AccountingError:
            logger.warning("OSPool usage import failed for %s", grant.ospool_project_name, exc_info=True)
            failed.append(grant.ospool_project_name)
        session.commit()
    return failed


# --------------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """A grant's usage to date, for its project page, the admin overview and the funder packet."""

    gpu_hours: Decimal
    core_hours: Decimal
    jobs: int
    #: Every valued day's worth, in cents.
    value_cents: int
    #: Days with GPU-hours but no price yet, so `value_cents` leaves them out.
    unvalued_days: int
    #: The last 30 days that have rows, newest first.
    recent: list[OspoolUsageDay]
    first_day: date | None
    last_day: date | None


def summary(grant: OspoolGrant) -> Summary:
    days = sorted(grant.usage_days, key=lambda row: row.day)
    return Summary(
        gpu_hours=sum((row.gpu_hours for row in days), Decimal(0)),
        core_hours=sum((row.core_hours for row in days), Decimal(0)),
        jobs=sum(row.jobs for row in days),
        value_cents=sum(row.value_cents or 0 for row in days),
        unvalued_days=sum(1 for row in days if row.value_cents is None and row.gpu_hours),
        recent=days[::-1][:30],
        first_day=days[0].day if days else None,
        last_day=days[-1].day if days else None,
    )


def _dollars(cents: int) -> str:
    whole, remainder = divmod(cents, 100)
    return f"${whole:,}.{remainder:02d}"


def _hours(hours: Decimal) -> str:
    return f"{hours.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,}"


def usage_text(grant: OspoolGrant) -> str:
    """One line on the grant's usage to date, for the funder packet. Only what OSG's public accounting already shows,
    plus Krater's valuation."""
    totals = summary(grant)
    since = grant.counted_from.isoformat()
    if totals.last_day is None:
        return f"{grant.ospool_project_name}: no usage reported since {since}."
    text = (
        f"{grant.ospool_project_name}: {_hours(totals.gpu_hours)} GPU-hours, {_hours(totals.core_hours)} core-hours "
        f"and {totals.jobs:,} jobs from {since} to {totals.last_day.isoformat()}. "
        f"Worth {_dollars(totals.value_cents)} at {rate_text(grant)}"
    )
    if totals.unvalued_days:
        text += f", leaving out {totals.unvalued_days} day{'s' if totals.unvalued_days != 1 else ''} with no price"
    return text + "."


def rate_text(grant: OspoolGrant) -> str:
    """How the grant's next days are valued, for display: "$2.49/GPU-hour (Lambda's posted price)"."""
    if grant.rate_override_microusd is None:
        return f"Krater's on-demand price for {grant.gpu_name}"
    dollars = (Decimal(grant.rate_override_microusd) / 1_000_000).normalize()
    price = f"${dollars:.2f}" if dollars == dollars.quantize(Decimal("0.01")) else f"${dollars:f}"
    return f"{price}/GPU-hour for {grant.gpu_name}" + (f" ({grant.rate_note})" if grant.rate_note else "")


__all__ = [
    "AUDIT_RECORD",
    "AUDIT_VALUATION",
    "REIMPORT_DAYS",
    "SOURCE_ADMIN",
    "Summary",
    "all_grants",
    "for_project",
    "import_all",
    "import_usage",
    "project_ids_with_grants",
    "rate_text",
    "record_grant",
    "summary",
    "update_valuation",
    "usage_text",
    "value_cents",
]
