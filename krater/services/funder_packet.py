"""The funder packet: the proposals an admin shares with a funder, for now OSPool (docs/SPEC.md "Funder packet").

The admin decides what goes to OSPool. The page lists every project, whatever its status, and the admin ticks the
ones to send; Krater makes them into a PDF the admin sends themselves. The packet is what leaves Krater, so the rules
here decide what a funder sees.

- **Which projects.** Exactly the ones the admin ticked. Nothing is filtered or ticked for them: the list shows each
  project's status and whether its whole team is verified US residents (OSPool takes only US residents), and the
  admin chooses.
- **Which fields.** Title, summary, how the team will use the compute, status, the amount requested and granted, the
  GPU estimate, the team's names, and whether every member is a verified US resident. Names follow the public
  gallery's rule for members under 18 (`krater.services.people.public_names`). Never emails, Patchwork IDs,
  demographics, secrets, logs or anything else: a field is shared only when it is added here.
- **Fresh or not.** The export reads names and residency fresh from Weave and fails closed when Weave can't be
  reached (`FunderPacketUnavailable`). The admin's page view may use the directory cache, and still lists every
  project when Weave is down.
- **Audit.** Every export writes an `AuditEvent` with the projects in it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import Project, ProjectRevision, ProjectStatus
from krater.services import audit, budget, people, residency, site_settings
from krater.services.actor import Actor
from krater.services.errors import FunderPacketUnavailable, NotAllowed, ValidationFailed
from krater.services.guardian import team_users
from krater.services.residency import TeamResidency
from krater.weave import WeaveClient, WeaveUnavailableError

AUDIT_EXPORT = "funder_packet_export"

FUNDER_OSPOOL = "ospool"
FORMAT_PDF = "pdf"

STATUS_LABELS = {
    ProjectStatus.DRAFT: "Draft, not submitted",
    ProjectStatus.PENDING_REVIEW: "Waiting for review",
    ProjectStatus.CHANGES_REQUESTED: "Changes requested",
    ProjectStatus.APPROVED: "Approved",
    ProjectStatus.PENDING_COMPLETION_REVIEW: "Approved, completion in review",
    ProjectStatus.COMPLETION_CHANGES_REQUESTED: "Approved, completion in review",
    ProjectStatus.COMPLETED: "Completed",
    ProjectStatus.WITHDRAWN: "Withdrawn",
}


@dataclass(frozen=True)
class GpuEstimate:
    """The proposal's compute estimate, as the team made it on the application form."""

    count: int
    name: str
    hours: float
    interruptible: bool

    @property
    def text(self) -> str:
        """`"1× RTX3060 × 10h, interruptible"`."""
        kind = "interruptible" if self.interruptible else "on-demand"
        return f"{self.count}× {self.name} × {self.hours:g}h, {kind}"


@dataclass(frozen=True)
class Candidate:
    """One project on the admin's list. Only for the admin's page: none of it leaves Krater."""

    project_id: uuid.UUID
    title: str
    summary: str
    status: ProjectStatus
    requested_cents: int
    granted_cents: int
    #: `None` when Weave couldn't be reached for the page view.
    residency: TeamResidency | None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class PacketRow:
    """One project as a funder sees it. Everything here may leave Krater."""

    project_id: uuid.UUID
    title: str
    summary: str
    compute_use: str
    training_data: str
    status: ProjectStatus
    requested_cents: int
    granted_cents: int
    gpu: GpuEstimate | None
    team_names: list[str]
    #: Team members the packet doesn't name (Weave didn't return them, or the gallery rule hides minors' names).
    unnamed: int
    team_size: int
    #: Every team member is a verified US resident in Weave, checked when the packet was made.
    all_us_residents: bool

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class Packet:
    funder: str
    generated_at: datetime
    rows: list[PacketRow]


def _require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise NotAllowed("Only an admin may share proposals with funders.")


def _revision(project: Project) -> ProjectRevision | None:
    # An approved project shows what was approved, not an amendment still in review; any other shows its newest.
    return project.approved_revision or project.current_revision


def candidates(session: Session, weave_client: WeaveClient, actor: Actor) -> list[Candidate]:
    """Every project, newest first, for the admin to pick from. Admin only. Uses the directory cache, and gives
    `residency=None` for a project whose team Weave couldn't be asked about."""
    _require_admin(actor)
    found = session.scalars(sa.select(Project).order_by(Project.created_at.desc()))
    rows = []
    for project in found:
        revision = _revision(project)
        rows.append(
            Candidate(
                project_id=project.id,
                title=project.title,
                summary=revision.summary if revision else "",
                status=project.status,
                requested_cents=revision.budget_requested_cents if revision else 0,
                granted_cents=budget.ceiling_cents(session, project),
                residency=residency.team_status_for_display(session, weave_client, project),
            )
        )
    return rows


def build(
    session: Session,
    weave_client: WeaveClient,
    actor: Actor,
    *,
    project_ids: Collection[uuid.UUID],
    now: datetime | None = None,
) -> Packet:
    """The OSPool packet of the projects the admin chose (`project_ids`; ids Krater doesn't know are ignored). Admin
    only. Reads names and residency fresh from Weave.

    Raises `FunderPacketUnavailable` when Weave can't be reached."""
    _require_admin(actor)
    chosen = list(
        session.scalars(sa.select(Project).where(Project.id.in_(list(project_ids))).order_by(Project.created_at.desc()))
    )
    minors = site_settings.current(session).gallery_minor_names

    rows: list[PacketRow] = []
    try:
        for project in chosen:
            team = team_users(session, project)
            names, unnamed = people.public_names(weave_client, team, minors=minors)
            residents = bool(team) and not residency.non_residents(session, weave_client, project, fresh=True)
            rows.append(
                _row(session, project, team_names=names, unnamed=unnamed, team_size=len(team), residents=residents)
            )
    except WeaveUnavailableError as exc:
        raise FunderPacketUnavailable() from exc
    return Packet(funder=FUNDER_OSPOOL, generated_at=now or datetime.now(UTC), rows=rows)


def _row(
    session: Session, project: Project, *, team_names: list[str], unnamed: int, team_size: int, residents: bool
) -> PacketRow:
    revision = _revision(project)
    return PacketRow(
        project_id=project.id,
        title=project.title,
        summary=revision.summary if revision else "",
        compute_use=revision.compute_use if revision else "",
        training_data=revision.training_data if revision else "",
        status=project.status,
        requested_cents=revision.budget_requested_cents if revision else 0,
        granted_cents=budget.ceiling_cents(session, project),
        gpu=_gpu(revision.budget_estimate if revision else None),
        team_names=team_names,
        unnamed=unnamed,
        team_size=team_size,
        all_us_residents=residents,
    )


def _gpu(estimate: dict | None) -> GpuEstimate | None:
    if not estimate or not estimate.get("accelerator_name"):
        return None
    return GpuEstimate(
        count=int(estimate.get("accelerator_count") or 1),
        name=str(estimate["accelerator_name"]),
        hours=float(estimate.get("hours") or 0),
        interruptible=estimate.get("basis") == "spot",
    )


def record_export(session: Session, actor: Actor, packet: Packet) -> None:
    """Audit an export of `packet` as a PDF. Refuses an empty packet: there is nothing to send. Flushes, doesn't
    commit."""
    _require_admin(actor)
    if not packet.rows:
        raise ValidationFailed({"project_id": "Tick at least one project to send."})
    audit.record(
        session,
        actor,
        AUDIT_EXPORT,
        payload={
            "funder": packet.funder,
            "format": FORMAT_PDF,
            "project_ids": [str(row.project_id) for row in packet.rows],
        },
    )


__all__ = [
    "AUDIT_EXPORT",
    "FORMAT_PDF",
    "FUNDER_OSPOOL",
    "STATUS_LABELS",
    "Candidate",
    "GpuEstimate",
    "Packet",
    "PacketRow",
    "build",
    "candidates",
    "record_export",
]
