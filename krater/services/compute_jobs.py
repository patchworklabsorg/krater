"""Submitting and cancelling Krater Compute jobs. The scheduler (`krater.compute.scheduler`) does the renting.

Members choose their own target and max prices, within their project's hourly cap. Cancelling only changes the job's
state; the scheduler destroys any machine a finished job still has on its next pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from krater import providers
from krater.config import get_settings
from krater.models import (
    ComputeEscalation,
    ComputeEvent,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    ComputeOutbid,
    Project,
)
from krater.models.compute_job import FINISHED_JOB_STATES
from krater.services import teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.services.launch_rules import LAUNCHABLE_STATUSES

MICROUSD_PER_CENT = 10_000
#: How a stop the member asked for is described; a stop Krater decided on (idle, a silent runner) gets a Slack notice.
MEMBER_STOP_PREFIX = "stopped by "
#: `actor=None` audit events about a member's box, posted to the project channel by `krater.services.slack_notify`.
AUDIT_BOX_STOPPED = "compute_box_stopped"
AUDIT_BOX_DESTROY_SOON = "compute_box_destroy_soon"
AUDIT_BOX_DESTROYED = "compute_box_destroyed"
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:@]*$")


@dataclass(frozen=True)
class JobSpec:
    """What a member asks for. Prices are full hourly cost (GPU plus disk), in micro-dollars."""

    name: str
    image: str
    target_microusd: int
    max_microusd: int
    command: str | None = None
    kind: ComputeJobKind = ComputeJobKind.JOB
    gpu_names: tuple[str, ...] = ()
    min_gpu_ram_gb: int | None = None
    num_gpus: int = 1
    disk_gb: int = 32
    interruptible: bool = True
    max_wait_minutes: int = 12 * 60
    on_max_wait: ComputeEscalation = ComputeEscalation.KEEP_WAITING
    max_runtime_minutes: int = 8 * 60
    on_outbid: ComputeOutbid = ComputeOutbid.MOVE


def _can_launch_for(actor: Actor, project: Project) -> bool:
    return teams.on_team(project, actor.user.id)


def _validate(spec: JobSpec, *, cap_microusd: int) -> None:
    errors: dict[str, str] = {}
    if not spec.name.strip() or len(spec.name) > 200:
        errors["name"] = "Give the job a name of up to 200 characters."
    if not _IMAGE_RE.match(spec.image) or len(spec.image) > 500:
        errors["image"] = "Enter a container image, such as ghcr.io/you/train:v1."
    if spec.kind is ComputeJobKind.JOB and not (spec.command and spec.command.strip()):
        errors["command"] = "Enter the command to run."
    if spec.command is not None and len(spec.command) > 2000:
        errors["command"] = "The command can be up to 2,000 characters; put longer scripts in the image."
    if not 1 <= spec.num_gpus <= 8:
        errors["num_gpus"] = "Choose 1 to 8 GPUs."
    if not 8 <= spec.disk_gb <= 2000:
        errors["disk_gb"] = "Choose 8 to 2,000 GB of disk."
    if not spec.gpu_names and spec.min_gpu_ram_gb is None:
        errors["gpu_names"] = "Choose GPU models, or a minimum amount of GPU memory."
    if spec.min_gpu_ram_gb is not None and not 1 <= spec.min_gpu_ram_gb <= 256:
        errors["min_gpu_ram_gb"] = "Choose 1 to 256 GB of GPU memory."
    if spec.target_microusd <= 0:
        errors["target"] = "Enter a target price above zero."
    elif spec.target_microusd > spec.max_microusd:
        errors["target"] = "The target can't be above the max price."
    if spec.max_microusd > cap_microusd:
        errors["max"] = f"The project's hourly cap is ${cap_microusd / 1_000_000:.2f}/h."
    if not 1 <= spec.max_wait_minutes <= 7 * 24 * 60:
        errors["max_wait_minutes"] = "Choose a max wait of 1 minute to 7 days."
    if not 1 <= spec.max_runtime_minutes <= 7 * 24 * 60:
        errors["max_runtime_minutes"] = "Choose a max runtime of 1 minute to 7 days."
    if spec.on_outbid is ComputeOutbid.HOLD and not spec.interruptible:
        errors["on_outbid"] = "Only interruptible machines are outbid: a limit order needs interruptible."
    _check_provider(spec, errors)
    if errors:
        raise ValidationFailed(errors)


def _check_provider(spec: JobSpec, errors: dict[str, str]) -> None:
    """Refuse what the provider in use can't do (`krater.providers.Capabilities`)."""
    provider = providers.active(get_settings())
    if provider is None:
        return
    can, title = provider.capabilities, provider.title
    if spec.kind is ComputeJobKind.BOX and not can.boxes:
        errors["kind"] = f"{title} machines can't be boxes: run a job instead."
    if spec.interruptible and not can.interruptible:
        errors["interruptible"] = f"{title} has no interruptible machines."
    if not spec.interruptible and not can.on_demand:
        errors["interruptible"] = f"{title} only has interruptible machines."
    if spec.on_outbid is ComputeOutbid.HOLD and not (can.bids and can.outbid_pauses):
        errors.setdefault("on_outbid", f"{title} can't hold a limit order: use a target and max price.")
    if spec.on_max_wait is ComputeEscalation.ON_DEMAND and not can.on_demand:
        errors["on_max_wait"] = f"{title} has no on-demand machines to switch to."
    if spec.on_max_wait is ComputeEscalation.RAISE_BID and not can.bids:
        errors["on_max_wait"] = f"{title} sets its own prices: there is no bid to raise."


def submit_job(
    session: Session, actor: Actor, *, project: Project, spec: JobSpec, cap_cents: int, now: datetime
) -> ComputeJob:
    """Queue `spec` for `project`. Only the project's team (lead and accepted members) may, and only while the project
    can launch. `cap_cents` is the project's hourly cap (`launch_rules.hourly_cap_cents`)."""
    if not _can_launch_for(actor, project):
        raise NotAllowed("Only the project's team can run jobs on its budget.")
    if project.status not in LAUNCHABLE_STATUSES:
        raise InvalidState(f"A {project.status.value} project can't run jobs.")
    _validate(spec, cap_microusd=cap_cents * MICROUSD_PER_CENT)
    job = ComputeJob(
        project_id=project.id,
        submitter_id=actor.user.id,
        kind=spec.kind,
        name=spec.name.strip(),
        image=spec.image,
        command=spec.command,
        gpu_names=list(spec.gpu_names),
        min_gpu_ram_gb=spec.min_gpu_ram_gb,
        num_gpus=spec.num_gpus,
        disk_gb=spec.disk_gb,
        target_microusd=spec.target_microusd,
        max_microusd=spec.max_microusd,
        interruptible=spec.interruptible,
        max_wait_minutes=spec.max_wait_minutes,
        on_max_wait=spec.on_max_wait,
        max_runtime_minutes=spec.max_runtime_minutes,
        on_outbid=spec.on_outbid,
        state=ComputeJobState.QUEUED,
        state_reason="Waiting for an offer at or under its "
        + (
            "limit."
            if spec.on_outbid is ComputeOutbid.HOLD and spec.target_microusd == spec.max_microusd
            else "target price."
        ),
        queued_at=now,
    )
    session.add(job)
    session.flush()
    session.add(ComputeEvent(job_id=job.id, kind="queued", message="Queued.", payload={}, at=now))
    session.flush()
    return job


def _who(actor: Actor, job: ComputeJob) -> str:
    """How a job's history names who acted. Not their name: Krater stores none (docs/weave-integration.md).
    The event payload keeps the actor's user id."""
    return "a team member" if _can_launch_for(actor, job.project) else "an admin"


def cancel_job(session: Session, actor: Actor, *, job: ComputeJob, now: datetime) -> ComputeJob:
    """Cancel `job`, by its team or an admin. Its machine, if any, is destroyed on the scheduler's next pass."""
    if not (actor.is_admin or _can_launch_for(actor, job.project)):
        raise NotAllowed("Only the project's team or an admin can cancel this job.")
    if job.state in FINISHED_JOB_STATES:
        raise InvalidState(f"The job is already {job.state.value}.")
    job.state = ComputeJobState.CANCELLED
    job.state_reason = f"Cancelled by {_who(actor, job)}."
    job.ended_at = now
    job.hold_cents = 0
    session.add(
        ComputeEvent(
            job_id=job.id, kind="cancelled", message=job.state_reason, payload={"by": str(actor.user.id)}, at=now
        )
    )
    session.flush()
    return job


# -- Boxes ---------------------------------------------------------------------------------------------------


def _live_instance(job: ComputeJob):
    live = [row for row in job.instances if row.is_live]
    return live[-1] if live else None


def _box_for(actor: Actor, job: ComputeJob) -> None:
    if job.kind is not ComputeJobKind.BOX:
        raise InvalidState("Only boxes can be stopped and resumed.")
    if not (actor.is_admin or _can_launch_for(actor, job.project)):
        raise NotAllowed("Only the project's team or an admin can do that to this box.")


def stop_box(session: Session, actor: Actor, *, job: ComputeJob, now: datetime) -> None:
    """Stop a running box: its runner saves /workspace, then the scheduler stops the machine (the disk is kept)."""
    _box_for(actor, job)
    row = _live_instance(job)
    if job.state is not ComputeJobState.RUNNING or row is None:
        raise InvalidState("Only a running box can be stopped.")
    if row.stop_requested_at is None:
        row.stop_requested_at = now
        row.stop_reason = f"{MEMBER_STOP_PREFIX}{_who(actor, job)}"
        session.add(
            ComputeEvent(
                job_id=job.id,
                instance_id=row.id,
                kind="stop_requested",
                message=row.stop_reason,
                payload={"by": str(actor.user.id)},
                at=now,
            )
        )
    session.flush()


def resume_box(session: Session, actor: Actor, *, job: ComputeJob, now: datetime) -> None:
    """Ask the scheduler to start a stopped box on its machine again (only works while the machine's GPU is free)."""
    _box_for(actor, job)
    row = _live_instance(job)
    if job.state is not ComputeJobState.STOPPED or row is None:
        raise InvalidState("Only a stopped box can be resumed.")
    if job.project.status not in LAUNCHABLE_STATUSES:
        raise InvalidState(f"A {job.project.status.value} project can't run boxes.")
    row.start_requested_at = now
    job.state_reason = "Resuming on the same machine."
    session.add(
        ComputeEvent(
            job_id=job.id, instance_id=row.id, kind="resume_requested", message=job.state_reason, payload={}, at=now
        )
    )
    session.flush()


def move_box(session: Session, actor: Actor, *, job: ComputeJob, now: datetime) -> None:
    """Give up a stopped box's machine and start it elsewhere from its last saved workspace. The member's choice:
    Krater never destroys a stopped box's disk on its own before the destroy time, since it may hold newer work."""
    from krater.compute.scheduler import MOVED_BY_MEMBER

    _box_for(actor, job)
    row = _live_instance(job)
    if job.state is not ComputeJobState.STOPPED or row is None:
        raise InvalidState("Only a stopped box can be moved.")
    row.destroy_requested_at = now
    row.destroy_reason = MOVED_BY_MEMBER
    job.state_reason = "Moving: starting elsewhere from the last saved workspace."
    session.add(
        ComputeEvent(
            job_id=job.id, instance_id=row.id, kind="move_requested", message=job.state_reason, payload={}, at=now
        )
    )
    session.flush()
