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
from krater.services import audit
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
        "reconcile_alert_percent",
        "Reconciliation alert",
        "Admins are alerted when Vast's hourly charges differ from Krater's estimate by more than this.",
        "%",
        1,
        100,
        10,
    ),
)
FIELDS_BY_NAME = {f.name: f for f in POLICY_FIELDS}


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
    #: The fields a project overrides (empty for the global policy).
    overridden: frozenset[str] = frozenset()

    @property
    def reliability_floor(self) -> float:
        """As Vast's search filter takes it (0.95)."""
        return self.reliability_floor_bp / 10_000

    def values(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in POLICY_FIELDS}


assert {f.name for f in fields(ComputePolicy)} - {"overridden"} == set(FIELDS_BY_NAME)


def format_value(field: PolicyField, value: int | None) -> str:
    """The value as an admin enters it: `0.95` for basis points, the integer otherwise. Blank for `None`."""
    if value is None:
        return ""
    if field.kind == "bp":
        return f"{Decimal(value) / 10_000:f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def parse_value(field: PolicyField, raw: str) -> int | None:
    """Parse a form value: blank -> `None`; otherwise an int (or a fraction for basis points). Raises
    `ValidationFailed` for anything else or out of bounds."""
    text = raw.strip()
    if not text:
        return None
    try:
        if field.kind == "bp":
            amount = Decimal(text) * 10_000
            if amount != amount.to_integral_value():
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
    return {f.name: f.default for f in POLICY_FIELDS}


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
    Admin only; `reason` required. Validates the resulting effective policy."""
    _require_admin_and_reason(actor, reason)
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
