"""Krater Compute's policy numbers: the global defaults, per-project overrides, and a project's effective policy.

Every number the design leaves to admins lives here (reliability floor, bid margin, pause grace, move time, box idle
and destroy times, the reconciliation alert threshold). Admins change the defaults and any project's overrides on
`/admin/compute`; each change writes an `AuditEvent` with the old and new values. The scheduler and the other
compute loops read `effective_policy(session, project)` and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeSettings, Project, ProjectComputeSettings
from krater.models.compute_settings import GLOBAL_ID
from krater.services import audit, teams
from krater.services.actor import Actor
from krater.services.errors import NotAllowed, ValidationFailed

AUDIT_GLOBAL = "compute_settings_update"
AUDIT_PROJECT = "compute_settings_override"


@dataclass(frozen=True)
class PolicyField:
    name: str
    label: str
    help: str
    unit: str
    minimum: int
    maximum: int
    default: int
    # "bp": stored in basis points, shown and entered as a fraction (9500 <-> 0.95).
    # "usd_per_tb": stored in micro-dollars per GB, shown and entered in dollars per TB (20000 <-> 20).
    # "usd": stored in cents, shown and entered in dollars (12550 <-> 125.5).
    kind: str = "int"


#: The policy numbers, in display order. Defaults are the values approved on 2026-10-11.
POLICY_FIELDS: tuple[PolicyField, ...] = (
    PolicyField(
        "reliability_floor_bp",
        "Reliability floor",
        "Krater only rents hosts Vast rates at least this reliable (0.95 = 95%).",
        "",
        5000,
        10000,
        9500,
        kind="bp",
    ),
    PolicyField(
        "bid_margin_percent",
        "Bid margin",
        "Krater bids an offer's minimum bid plus this much, never above the job's max price.",
        "%",
        0,
        100,
        10,
    ),
    PolicyField(
        "pause_grace_minutes",
        "Pause grace",
        "How long an outbid job waits before Krater raises its bid.",
        "minutes",
        0,
        1440,
        5,
    ),
    PolicyField(
        "move_after_minutes",
        "Move after",
        "How long a job may stay paused before Krater moves it to another machine.",
        "minutes",
        1,
        10080,
        30,
    ),
    PolicyField(
        "box_idle_minutes",
        "Box idle",
        "A box with no GPU or CPU use and no open session for this long is stopped.",
        "minutes",
        5,
        1440,
        30,
    ),
    PolicyField(
        "box_destroy_hours",
        "Destroy stopped box after",
        "A stopped box is destroyed (its disk too) after this long. Its workspace is in storage.",
        "hours",
        1,
        720,
        24,
    ),
    PolicyField(
        "runner_start_minutes",
        "Runner start deadline",
        "A running machine whose runner hasn't reported in this long is destroyed and its job failed (the image "
        "needs a POSIX shell and curl, wget or Python).",
        "minutes",
        2,
        120,
        10,
    ),
    PolicyField(
        "heartbeat_timeout_minutes",
        "Heartbeat timeout",
        "A machine whose runner stops sending heartbeats for this long is destroyed and its job failed. Keep it "
        "well above the time Krater itself may be down for a deploy.",
        "minutes",
        5,
        1440,
        30,
    ),
    PolicyField(
        "runner_self_stop_minutes",
        "Self-stop without Krater",
        "A machine whose runner can't reach Krater for this long stops itself, keeping its disk, so nothing bills "
        "unwatched while Krater is down. When Krater is back it starts a job's machine again (the job carries on "
        "from the checkpoints on its disk) and shows a box as stopped. Keep it well above the time Krater may be "
        "down for a deploy.",
        "minutes",
        30,
        10080,
        180,
    ),
    PolicyField(
        "box_sync_deadline_minutes",
        "Box sync deadline",
        "Before stopping a box, Krater asks its runner to save /workspace, and stops it anyway after this long.",
        "minutes",
        1,
        120,
        10,
    ),
    PolicyField(
        "box_resume_timeout_minutes",
        "Box resume timeout",
        "A stopped box restarts only if its machine's GPU is free. If it isn't running this long after the member "
        "asks, Krater tells them and offers to start it elsewhere from its last saved workspace.",
        "minutes",
        2,
        120,
        10,
    ),
    PolicyField(
        "storage_quota_gb",
        "Storage quota",
        "Checkpoints and outputs one project's machines may keep in Krater's storage. Uploads past it are refused.",
        "GB",
        1,
        10000,
        50,
    ),
    PolicyField(
        "transfer_cap_microusd_per_gb",
        "Data transfer price cap",
        "Vast bills data into and out of a machine per GB, at the host's own price, on top of the hourly cost. "
        "Krater rents no host that charges more than this, each way. On 2026-11-04 the median verified host "
        "charged $3.91 per TB and one in ten charged $39 to $80; $20 kept four in five offers.",
        "USD per TB",
        0,
        1_000_000,
        20_000,
        kind="usd_per_tb",
    ),
    PolicyField(
        "transfer_warn_percent",
        "Data transfer warning",
        "Members are warned when a job's data transfer could cost this share of the project's budget: before it is "
        "queued, from their own estimate at the price cap, and while it runs, from Vast's charges.",
        "% of budget",
        1,
        100,
        10,
    ),
    PolicyField(
        "reconcile_alert_percent",
        "Reconciliation alert",
        "Admins are alerted when Vast's hourly charges differ from Krater's estimate by more than this.",
        "%",
        1,
        100,
        10,
    ),
    PolicyField(
        "max_machines",
        "Machines at once",
        "The most machines one project may have running, starting or outbid at once. More jobs wait in the queue "
        "until one ends. A stopped box doesn't count, but resuming one needs a free place.",
        "machines",
        1,
        100,
        4,
    ),
    PolicyField(
        "job_idle_warn_minutes",
        "Idle GPU warning",
        "A batch job whose GPU stays under 5% use this long gets one warning in its project's channel (often the code "
        "isn't using the GPU, or it's stuck). It keeps running. 0 turns the warning off.",
        "minutes",
        0,
        1440,
        30,
    ),
)
FIELDS_BY_NAME = {f.name: f for f in POLICY_FIELDS}

#: Organization-wide limits: one value for all of Krater, no per-project override. 0 turns each one off, and the
#: spending caps and the credit alert are off until an admin sets them.
ORG_FIELDS: tuple[PolicyField, ...] = (
    PolicyField(
        "org_daily_cap_cents",
        "Daily spending cap",
        "Krater's compute spend across every project in one UTC day. When it's reached, Krater stops every machine "
        "(as the stop switch does) until midnight UTC, or until an admin raises it. Spend is Krater's estimate, which "
        "can trail Vast's charges by up to an hour, so keep a margin. 0 = no cap.",
        "USD",
        0,
        100_000_000,
        0,
        kind="usd",
    ),
    PolicyField(
        "org_monthly_cap_cents",
        "Monthly spending cap",
        "The same for a calendar month (UTC). 0 = no cap.",
        "USD",
        0,
        100_000_000,
        0,
        kind="usd",
    ),
    PolicyField(
        "credit_alert_cents",
        "Low credit alert",
        "Admins are alerted in Slack when the Vast account's credit falls under this. Krater checks it every hour. "
        "0 = no alert.",
        "USD",
        0,
        100_000_000,
        0,
        kind="usd",
    ),
    PolicyField(
        "finished_files_days",
        "Delete finished projects' files after",
        "A completed or withdrawn project's checkpoints and outputs are deleted this many days after it finishes, so "
        "storage doesn't fill up. The date is fixed when the project finishes and posted in its channel. Changing "
        "this doesn't move dates already set. 0 = keep them.",
        "days",
        0,
        3650,
        30,
    ),
    PolicyField(
        "credit_reserve_hours",
        "Credit reserve",
        "Krater rents no new machine while the Vast account's credit wouldn't cover this many hours of every machine "
        "it holds plus the new one, so the account doesn't run dry mid-run. Jobs wait, saying why. Needs a credit "
        "reading less than 2 hours old; without one Krater rents as usual. 0 = off.",
        "hours",
        0,
        168,
        0,
    ),
    PolicyField(
        "dm_job_endings",
        "Message the submitter when a job ends",
        "0 = never, 1 = when a job fails, 2 = whenever a job ends. The message goes from Krater's Slack app to whoever "
        "queued the job, as its verification reminders do, on top of the notice in the project's channel.",
        "",
        0,
        2,
        0,
    ),
)
ORG_FIELDS_BY_NAME = {f.name: f for f in ORG_FIELDS}
AUDIT_ORG = "compute_org_limits_update"


@dataclass(frozen=True)
class ComputePolicy:
    """A project's (or the global) policy, every number resolved."""

    reliability_floor_bp: int
    bid_margin_percent: int
    pause_grace_minutes: int
    move_after_minutes: int
    box_idle_minutes: int
    box_destroy_hours: int
    reconcile_alert_percent: int
    runner_start_minutes: int
    heartbeat_timeout_minutes: int
    runner_self_stop_minutes: int
    storage_quota_gb: int
    box_sync_deadline_minutes: int
    box_resume_timeout_minutes: int
    transfer_cap_microusd_per_gb: int
    transfer_warn_percent: int
    max_machines: int
    job_idle_warn_minutes: int
    #: The fields a project overrides (empty for the global policy).
    overridden: frozenset[str] = frozenset()

    @property
    def reliability_floor(self) -> float:
        """As Vast's search filter takes it (0.95)."""
        return self.reliability_floor_bp / 10_000

    def values(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in POLICY_FIELDS}


assert {f.name for f in fields(ComputePolicy)} - {"overridden"} == set(FIELDS_BY_NAME)


#: What a scaled kind's stored integer is divided by to show it: basis points per 1, micro-dollars per GB per $/TB.
_SCALED = {"bp": 10_000, "usd_per_tb": 1_000, "usd": 100}


def format_value(field: PolicyField, value: int | None) -> str:
    """The value as an admin enters it: `0.95` for basis points, the integer otherwise. Blank for `None`."""
    if value is None:
        return ""
    if field.kind in _SCALED:
        text = f"{Decimal(value) / _SCALED[field.kind]:f}"
        return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"
    return str(value)


def parse_value(field: PolicyField, raw: str) -> int | None:
    """Parse a form value: blank -> `None`; otherwise an int (or a fraction for basis points). Raises
    `ValidationFailed` for anything else or out of bounds."""
    text = raw.strip()
    if not text:
        return None
    try:
        if field.kind in _SCALED:
            amount = Decimal(text) * _SCALED[field.kind]
            if not amount.is_finite() or amount != amount.to_integral_value():
                raise ValueError
            value = int(amount)
        else:
            value = int(text)
    except (ValueError, ArithmeticError):
        raise ValidationFailed({field.name: f"Enter a number for {field.label.lower()}."}) from None
    _check_bounds(field, value)
    return value


def _check_bounds(field: PolicyField, value: int) -> None:
    if not field.minimum <= value <= field.maximum:
        low, high = format_value(field, field.minimum), format_value(field, field.maximum)
        raise ValidationFailed({field.name: f"{field.label} must be between {low} and {high}."})


def _defaults() -> dict[str, int]:
    return {f.name: f.default for f in POLICY_FIELDS + ORG_FIELDS}


def _global_row(session: Session) -> ComputeSettings:
    row = session.get(ComputeSettings, GLOBAL_ID)
    if row is None:
        # The migration seeds it; recreate it with the approved defaults if it was ever deleted.
        row = ComputeSettings(id=GLOBAL_ID, **_defaults())
        session.add(row)
        session.flush()
    return row


def global_policy(session: Session) -> ComputePolicy:
    row = _global_row(session)
    return ComputePolicy(**{f.name: getattr(row, f.name) for f in POLICY_FIELDS})


def project_overrides(session: Session, project: Project) -> dict[str, int | None]:
    row = session.get(ProjectComputeSettings, project.id)
    return {f.name: (getattr(row, f.name) if row is not None else None) for f in POLICY_FIELDS}


def effective_policy(session: Session, project: Project) -> ComputePolicy:
    """`project`'s policy: each override where it has one, the global default otherwise."""
    base = global_policy(session).values()
    overrides = {k: v for k, v in project_overrides(session, project).items() if v is not None}
    return ComputePolicy(**{**base, **overrides}, overridden=frozenset(overrides))


def _validate(values: dict[str, int]) -> None:
    errors: dict[str, str] = {}
    for name, value in values.items():
        try:
            _check_bounds(FIELDS_BY_NAME[name], value)
        except ValidationFailed as exc:
            errors.update(exc.errors)
    if not errors and values["move_after_minutes"] < values["pause_grace_minutes"]:
        errors["move_after_minutes"] = "Move after must be at least the pause grace."
    if errors:
        raise ValidationFailed(errors)


def _require_admin_and_reason(actor: Actor, reason: str) -> None:
    if not actor.is_admin:
        raise NotAllowed("Only an admin may change compute settings.")
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})


def update_global(session: Session, actor: Actor, *, values: dict[str, int], reason: str) -> ComputePolicy:
    """Set the global defaults. `values` must carry every policy field. Admin only; `reason` required.
    The cross-field rule (move after >= pause grace) is checked against every project's effective policy
    too, so a default change can't leave an override inconsistent."""
    _require_admin_and_reason(actor, reason)
    missing = set(FIELDS_BY_NAME) - set(values)
    if missing:
        raise ValidationFailed({name: "Required." for name in sorted(missing)})
    _validate(values)
    for project, overrides in projects_with_overrides(session):
        resolved = {**values, **{k: v for k, v in overrides.items() if v is not None}}
        try:
            _validate(resolved)
        except ValidationFailed:
            title = project.title or "Untitled proposal"
            raise ValidationFailed(
                {"move_after_minutes": f"{title}'s overrides would then move jobs before their pause grace ends."}
            ) from None

    row = _global_row(session)
    before = {f.name: getattr(row, f.name) for f in POLICY_FIELDS}
    changed = {k: {"old": before[k], "new": v} for k, v in values.items() if before[k] != v}
    if not changed:
        return global_policy(session)
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_by_id = actor.user.id
    audit.record(session, actor, AUDIT_GLOBAL, payload={"changed": changed}, reason=reason)
    session.flush()
    return global_policy(session)


def set_project_overrides(
    session: Session, actor: Actor, *, project: Project, overrides: dict[str, int | None], reason: str
) -> ComputePolicy:
    """Replace `project`'s overrides with `overrides` (`None` = use the default; a field left out is cleared too).
    Admin only, and never on a project the admin leads, is on the team of or is invited to, as for budget and the
    hourly cap (`projects.admin_decide`): a bigger storage quota or a longer runtime is theirs to use. `reason`
    required. Validates the resulting effective policy."""
    _require_admin_and_reason(actor, reason)
    if teams.has_conflict(project, actor.user.id):
        raise NotAllowed(
            "Admins can't change the compute settings of a project they lead or are on the team of (or invited to). "
            "Ask another admin."
        )
    unknown = set(overrides) - set(FIELDS_BY_NAME)
    if unknown:
        raise ValidationFailed({name: "Unknown setting." for name in sorted(unknown)})
    new = {f.name: overrides.get(f.name) for f in POLICY_FIELDS}
    resolved = {**global_policy(session).values(), **{k: v for k, v in new.items() if v is not None}}
    _validate(resolved)

    before = project_overrides(session, project)
    changed = {k: {"old": before[k], "new": v} for k, v in new.items() if before[k] != v}
    if not changed:
        return effective_policy(session, project)

    row = session.get(ProjectComputeSettings, project.id)
    if row is None:
        row = ProjectComputeSettings(project_id=project.id)
        session.add(row)
    for name, value in new.items():
        setattr(row, name, value)
    row.updated_by_id = actor.user.id
    audit.record(session, actor, AUDIT_PROJECT, project=project, payload={"changed": changed}, reason=reason)
    session.flush()
    return effective_policy(session, project)


@dataclass(frozen=True)
class OrgLimits:
    org_daily_cap_cents: int
    org_monthly_cap_cents: int
    credit_alert_cents: int
    finished_files_days: int
    credit_reserve_hours: int
    dm_job_endings: int

    def values(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in ORG_FIELDS}


def org_limits(session: Session) -> OrgLimits:
    row = _global_row(session)
    return OrgLimits(**{f.name: getattr(row, f.name) for f in ORG_FIELDS})


def update_org_limits(session: Session, actor: Actor, *, values: dict[str, int], reason: str) -> OrgLimits:
    """Set the organization-wide limits. `values` must carry every `ORG_FIELDS` field. Admin only; `reason`
    required."""
    _require_admin_and_reason(actor, reason)
    missing = set(ORG_FIELDS_BY_NAME) - set(values)
    if missing:
        raise ValidationFailed({name: "Required." for name in sorted(missing)})
    errors: dict[str, str] = {}
    for name in ORG_FIELDS_BY_NAME:
        try:
            _check_bounds(ORG_FIELDS_BY_NAME[name], values[name])
        except ValidationFailed as exc:
            errors.update(exc.errors)
    if errors:
        raise ValidationFailed(errors)
    row = _global_row(session)
    changed = {
        name: {"old": getattr(row, name), "new": values[name]}
        for name in ORG_FIELDS_BY_NAME
        if getattr(row, name) != values[name]
    }
    if not changed:
        return org_limits(session)
    for name in ORG_FIELDS_BY_NAME:
        setattr(row, name, values[name])
    row.updated_by_id = actor.user.id
    audit.record(session, actor, AUDIT_ORG, payload={"changed": changed}, reason=reason)
    session.flush()
    return org_limits(session)


def projects_with_overrides(session: Session) -> list[tuple[Project, dict[str, int | None]]]:
    """Every project with at least one override, newest change first."""
    rows = session.scalars(sa.select(ProjectComputeSettings).order_by(ProjectComputeSettings.updated_at.desc()))
    result = []
    for row in rows:
        values = {f.name: getattr(row, f.name) for f in POLICY_FIELDS}
        if any(v is not None for v in values.values()):
            project = session.get(Project, row.project_id)
            if project is not None:
                result.append((project, values))
    return result
