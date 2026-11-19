"""Submitting and cancelling Krater Compute jobs. The scheduler (`krater.compute.scheduler`) does the renting.

Members choose their own target and max prices, within their project's hourly cap. Cancelling only changes the job's
state; the scheduler destroys any machine a finished job still has on its next pass.

Vast bills data into and out of a machine per GB on top of the hourly price. A member may estimate the data their
job moves; when that could cost a large share of the budget at the transfer price cap (the most Krater pays per GB),
submitting needs their confirmation (`transfer_estimate`, `ConfirmationRequired`).
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater import providers
from krater.config import get_settings
from krater.models import (
    ComputeEscalation,
    ComputeEvent,
    ComputeInstance,
    ComputeInstanceState,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    ComputeOutbid,
    Project,
)
from krater.models.compute_job import ACTIVE_JOB_STATES, FINISHED_JOB_STATES
from krater.registry import ImageUnavailable, Registry, RegistryError, WrongPlatform, parse_reference
from krater.services import (
    audit,
    budget,
    clearance,
    compute_halt,
    compute_inputs,
    compute_settings,
    funding_holds,
    image_checks,
    smoke,
    teams,
    workspaces,
)
from krater.services.actor import Actor
from krater.services.errors import ConfirmationRequired, InvalidState, NotAllowed, ValidationFailed
from krater.services.launch_rules import LAUNCHABLE_STATUSES
from krater.storage import ObjectStore
from krater.weave import WeaveClient

MICROUSD_PER_CENT = 10_000
BYTES_PER_GB = 1_000_000_000
#: How a stop the member asked for is described; a stop Krater decided on (idle, a silent runner) gets a Slack notice.
MEMBER_STOP_PREFIX = "stopped by "
#: `actor=None` audit events about a member's box, posted to the project channel by `krater.services.slack_notify`.
AUDIT_BOX_STOPPED = "compute_box_stopped"
AUDIT_BOX_DESTROY_SOON = "compute_box_destroy_soon"
AUDIT_BOX_DESTROYED = "compute_box_destroyed"
#: `actor=None`: a job's data transfer has cost a large share of its project's budget (`krater.compute.reconcile`).
AUDIT_TRANSFER_WARNING = "compute_transfer_warning"
#: A batch job ended (succeeded, failed, or was stopped by Krater or an admin): a notice in its project's channel.
AUDIT_JOB_ENDED = "compute_job_ended"
#: The highest spend cap a job can set: far above any real job, low enough to catch a typo's extra zeros.
MAX_SPEND_CAP_CENTS = 100_000_00
#: A batch job's GPU sat nearly unused for the policy's idle warning time.
AUDIT_JOB_IDLE = "compute_job_idle"
#: A running job's checkpoints stopped reaching storage (`krater.compute.checkpoint_watch`).
AUDIT_CHECKPOINT_OVERDUE = "compute_checkpoint_overdue"
#: How a member's own cancel reads (`cancel_job`): no channel notice for it, they know.
MEMBER_CANCEL_REASON = "Cancelled by a team member."
#: Machines that hold a place under a project's "machines at once" limit (`max_machines`): all but a stopped box.
LIMITED_STATES = (
    ComputeInstanceState.CREATING,
    ComputeInstanceState.PROVISIONING,
    ComputeInstanceState.RUNNING,
    ComputeInstanceState.PAUSED,
)
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:@]*$")
#: What the job form starts with: public, with a shell and Python, and PyTorch with CUDA for a GPU. A box that needs
#: none of that can use `ubuntu:24.04`, a much smaller pull. CUDA 12.8 or later, because the RTX 50 series (often the
#: cheapest offers) has no kernels in older PyTorch builds ("no kernel image is available"); this one ran on an RTX
#: 5060 Ti in the 2026-11-19 staging test.
DEFAULT_IMAGE = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime"
_EXAMPLE_IMAGES = f"such as {DEFAULT_IMAGE} or ubuntu:24.04"
#: A web address someone pasted for an image: Docker Hub's and GitHub's pages, or any URL.
_LINK_RE = re.compile(r"^(?:[a-z]+://|(?:www\.)?(?:hub\.docker\.com|github\.com)/)", re.IGNORECASE)


def _image_from_link(link: str) -> str | None:
    """The image a Docker Hub or GitHub package page is about, if `link` is one, else None."""
    path = re.sub(r"^[a-z]+://", "", link.strip(), flags=re.IGNORECASE).split("?")[0].split("#")[0].strip("/")
    host, _, path = path.partition("/")
    parts = [part for part in path.split("/") if part]
    host = host.lower().removeprefix("www.")
    if host == "hub.docker.com":
        if len(parts) >= 2 and parts[0] == "_":  # an official image: hub.docker.com/_/ubuntu
            return parts[1].lower()
        if len(parts) >= 3 and parts[0] == "r":  # hub.docker.com/r/pytorch/pytorch
            return f"{parts[1]}/{parts[2]}".lower()
        if len(parts) >= 4 and parts[0] == "layers":  # a tag's page: hub.docker.com/layers/<owner>/<name>/<tag>/...
            name = parts[2] if parts[1] == "library" else f"{parts[1]}/{parts[2]}"
            return f"{name}:{parts[3]}".lower()
    if host == "github.com" and len(parts) >= 5 and parts[2:4] == ["pkgs", "container"]:
        return f"ghcr.io/{parts[0]}/{parts[4]}".lower()  # github.com/<owner>/<repo>/pkgs/container/<name>
    return None


def image_error(image: str) -> str | None:
    """What's wrong with `image` as written (a link, a pasted `docker pull`, not a name Docker reads), or None."""
    if _LINK_RE.match(image):
        named = _image_from_link(image)
        if named:
            return f"Enter the image's name, not a web address: for that page, {named}, with a tag if you need one."
        return f"Enter the image's name, not a web address, {_EXAMPLE_IMAGES}."
    pull = re.match(r"^docker\s+pull\s+(\S+)$", image.strip(), re.IGNORECASE)
    if pull:
        return f"Enter only the image's name: {pull.group(1)}."
    if not _IMAGE_RE.match(image) or len(image) > 500:
        return f"Enter a container image's name, {_EXAMPLE_IMAGES}. Image names are lowercase."
    try:
        parse_reference(image)
    except RegistryError:
        return f"{image} isn't an image name Docker can read. Enter one {_EXAMPLE_IMAGES}."
    return None


def check_image(
    registry: Registry | None,
    image: str,
    *,
    spec: JobSpec | None = None,
    session: Session | None = None,
    now: datetime | None = None,
) -> image_checks.ImageReport:
    """Raise `ValidationFailed` when `image`'s registry says no one can pull it without signing in (it doesn't exist,
    the tag is wrong, or it's private): a machine would be rented only to be refused the same. In production
    (2026-11-12) a box named the guide's example image with a full stop after it. When the registry can't be asked
    (down, rate-limiting, a host Krater won't reach) the job goes ahead: the scheduler still fails it as soon as a
    machine's pull is refused (`krater.compute.scheduler.pull_refused`)."""
    if registry is None:
        return image_checks.ImageReport()
    try:
        details = image_checks.describe(registry, image, session=session, now=now or datetime.now(UTC))
    except WrongPlatform as exc:
        raise ValidationFailed({"image": f"{image} can't run on Krater's machines: {exc}"}) from None
    except ImageUnavailable as exc:
        raise ValidationFailed(
            {
                "image": f"{image} can't be pulled: {exc} Check its name and tag, and that it's public: machines "
                "pull it without signing in."
            }
        ) from None
    except RegistryError:
        details = None
    # Certain to fail: refused. Likely to: warnings (`krater.services.image_checks`).
    if details is not None and (problem := image_checks.platform_problem(details)) is not None:
        raise ValidationFailed({"image": f"{image} can't run on Krater's machines: {problem}"})
    if spec is not None and details is not None and details.size_bytes > spec.disk_gb * image_checks.BYTES_PER_GB:
        raise ValidationFailed(
            {
                "disk_gb": f"The image alone is {details.size_bytes / image_checks.BYTES_PER_GB:.1f} GB compressed, "
                f"more than {spec.disk_gb} GB of disk. Ask for at least "
                f"{math.ceil(details.size_bytes * image_checks.UNPACKED_FACTOR / image_checks.BYTES_PER_GB) + 10} GB."
            }
        )
    warnings = (
        image_checks.warnings_for(
            details, image, command=spec.command, gpu_names=tuple(spec.gpu_names), disk_gb=spec.disk_gb
        )
        if spec is not None
        else []
    )
    return image_checks.ImageReport(
        warnings=warnings,
        cuda=image_checks.cuda_version(details, image),
        size_bytes=details.size_bytes if details is not None else None,
    )


#: The event a job's image checks write for each warning (`krater.services.image_checks`).
IMAGE_WARNING = "image_warning"

#: The most data a job may say it moves each way, in GB (100 TB).
MAX_DATA_GB = 100_000
#: How often a job's runner uploads its changed checkpoint files, in minutes, unless the job chooses its own interval.
#: The same as `krater.compute.runner_api.CHECKPOINT_SYNC_SECONDS`, which a test keeps in step.
DEFAULT_CHECKPOINT_UPLOAD_MINUTES = 10
MAX_CHECKPOINT_UPLOAD_MINUTES = 60


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
    # The member's estimate of the data moved into and out of the machine, in GB; `None` for no estimate.
    data_in_gb: int | None = None
    data_out_gb: int | None = None
    # Names (or folders ending in /) from the project's inputs to copy onto the machine (`compute_inputs`).
    inputs: tuple[str, ...] = ()
    # A smoke test of this many minutes (`krater.services.smoke`); None for the full run.
    smoke_minutes: int | None = None
    # Upload changed checkpoint files every this many minutes; None for the default. More often loses less work when
    # the job has to move to another machine, and costs more data out (Vast bills it per GB).
    checkpoint_upload_minutes: int | None = None
    # Stop once the job has cost this many cents (a box: stop it); None for no cap of its own.
    max_spend_cents: int | None = None
    # A box's /workspace (`krater.services.workspaces`): continue this workspace of the project, or (None) start a new
    # one, named `workspace_name` (the box's name if blank). `end_holder`: end the stopped box that still holds the
    # workspace, so this one can continue it.
    workspace_id: uuid.UUID | None = None
    workspace_name: str | None = None
    end_holder: bool = False
    # Set from the image's checks, not by the member: the CUDA major version a machine's driver must support.
    min_cuda_major: int | None = None


@dataclass(frozen=True)
class TransferEstimate:
    """The most a job's estimated data transfer can cost: its GB in and out at the transfer price cap."""

    in_gb: int
    out_gb: int
    cap_microusd_per_gb: int
    cost_cents: int
    budget_cents: int
    remaining_cents: int
    warn_percent: int

    @property
    def percent_of_budget(self) -> int:
        return math.ceil(self.cost_cents * 100 / self.budget_cents) if self.budget_cents > 0 else 100

    @property
    def needs_confirmation(self) -> bool:
        if self.cost_cents <= 0:
            return False
        return self.percent_of_budget >= self.warn_percent or self.cost_cents > max(self.remaining_cents, 0)

    def message(self) -> str:
        per_tb = self.cap_microusd_per_gb / 1_000
        text = (
            f"This job's data transfer could cost up to ${self.cost_cents / 100:,.2f}: {self.in_gb:,} GB into the "
            f"machine and {self.out_gb:,} GB out, at up to ${per_tb:,.2f} per TB each way (the most Krater pays). "
        )
        if self.cost_cents > max(self.remaining_cents, 0):
            text += f"That's more than the ${max(self.remaining_cents, 0) / 100:,.2f} left in the project's budget. "
        else:
            text += f"That's {self.percent_of_budget}% of the project's ${self.budget_cents / 100:,.2f} budget. "
        return text + (
            "Vast bills it on top of the hourly price, and each move to another machine pulls the image and "
            "restores checkpoints again."
        )


def transfer_estimate(
    session: Session, project: Project, spec: JobSpec, *, inputs_bytes: int = 0
) -> TransferEstimate | None:
    """What `spec`'s estimated data transfer (with its inputs, `inputs_bytes`) could cost at most, or `None` without
    an estimate."""
    if spec.data_in_gb is None and spec.data_out_gb is None and not inputs_bytes:
        return None
    policy = compute_settings.effective_policy(session, project)
    in_gb = (spec.data_in_gb or 0) + math.ceil(inputs_bytes / BYTES_PER_GB)
    out_gb = spec.data_out_gb or 0
    cost_microusd = (in_gb + out_gb) * policy.transfer_cap_microusd_per_gb
    return TransferEstimate(
        in_gb=in_gb,
        out_gb=out_gb,
        cap_microusd_per_gb=policy.transfer_cap_microusd_per_gb,
        cost_cents=math.ceil(cost_microusd / MICROUSD_PER_CENT),
        budget_cents=budget.ceiling_cents(session, project),
        remaining_cents=budget.remaining_cents(session, project),
        warn_percent=policy.transfer_warn_percent,
    )


def inputs_size(store: ObjectStore | None, project: Project, spec: JobSpec) -> int:
    """The total size of the inputs `spec` asks for; raises `ValidationFailed` for one that isn't stored."""
    if not spec.inputs:
        return 0
    if store is None:
        raise ValidationFailed({"inputs": "Krater's storage isn't set up, so a job can't take inputs."})
    return compute_inputs.resolve(store, project.id, spec.inputs)


def _can_launch_for(actor: Actor, project: Project) -> bool:
    return teams.on_team(project, actor.user.id)


def validate_spec(
    spec: JobSpec, *, cap_cents: int, registry: Registry | None = None, session: Session | None = None
) -> image_checks.ImageReport:
    """Raise `ValidationFailed` for what `submit_job` would refuse in `spec` itself (for estimating before queueing),
    and, given a `registry`, for an image it refuses (`check_image`). Returns what the image's checks warn of."""
    _validate(spec, cap_microusd=cap_cents * MICROUSD_PER_CENT)
    return check_image(registry, spec.image, spec=spec, session=session)


def _validate(spec: JobSpec, *, cap_microusd: int) -> None:
    errors: dict[str, str] = {}
    if not spec.name.strip() or len(spec.name) > 200:
        errors["name"] = "Give the job a name of up to 200 characters."
    if (image_problem := image_error(spec.image)) is not None:
        errors["image"] = image_problem
    if spec.kind is ComputeJobKind.JOB and (spec.workspace_id is not None or spec.workspace_name or spec.end_holder):
        errors["workspace"] = "Only a box has a workspace."
    if spec.workspace_name:
        try:
            workspaces.check_name(spec.workspace_name)
        except ValidationFailed as exc:
            errors.update(exc.errors)
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
    for name, value, way in (("data_in_gb", spec.data_in_gb, "into"), ("data_out_gb", spec.data_out_gb, "out of")):
        if value is not None and not 0 <= value <= MAX_DATA_GB:
            errors[name] = f"Estimate 0 to {MAX_DATA_GB:,} GB of data {way} the machine, or leave it blank."
    if spec.on_outbid is ComputeOutbid.HOLD and not spec.interruptible:
        errors["on_outbid"] = "Only interruptible machines are outbid: a limit order needs interruptible."
    if spec.smoke_minutes is not None:
        if spec.kind is ComputeJobKind.BOX:
            errors["smoke_minutes"] = "A smoke test is for jobs: a box already runs until you stop it."
        elif not 1 <= spec.smoke_minutes <= smoke.MAX_SMOKE_MINUTES:
            errors["smoke_minutes"] = f"Choose a smoke test of 1 to {smoke.MAX_SMOKE_MINUTES} minutes."
        elif spec.smoke_minutes >= spec.max_runtime_minutes:
            errors["smoke_minutes"] = "The smoke test must be shorter than the max runtime."
    if spec.checkpoint_upload_minutes is not None:
        if spec.kind is ComputeJobKind.BOX:
            errors["checkpoint_upload_minutes"] = (
                f"A box saves /workspace every {DEFAULT_CHECKPOINT_UPLOAD_MINUTES} minutes; the upload interval is "
                "for jobs."
            )
        elif not 1 <= spec.checkpoint_upload_minutes <= MAX_CHECKPOINT_UPLOAD_MINUTES:
            errors["checkpoint_upload_minutes"] = (
                f"Choose a checkpoint upload every 1 to {MAX_CHECKPOINT_UPLOAD_MINUTES} minutes, or leave it blank for "
                f"every {DEFAULT_CHECKPOINT_UPLOAD_MINUTES}."
            )
    if spec.max_spend_cents is not None and not 1 <= spec.max_spend_cents <= MAX_SPEND_CAP_CENTS:
        errors["max_spend"] = (
            f"Choose a spend cap of $0.01 to ${MAX_SPEND_CAP_CENTS // 100:,}, or leave it blank for none."
        )
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
    session: Session,
    actor: Actor,
    *,
    project: Project,
    spec: JobSpec,
    cap_cents: int,
    now: datetime,
    weave_client: WeaveClient,
    confirm_transfer_cost: bool = False,
    store: ObjectStore | None = None,
    registry: Registry | None = None,
) -> ComputeJob:
    """Queue `spec` for `project`. Only the project's team (lead and accepted members) may, only while the project
    can launch, not while it has a funding hold (`funding_holds.require_not_held`), and only once Weave has cleared
    every team member (`clearance.require_team_cleared`, asking `weave_client` fresh). `cap_cents` is the project's
    hourly cap (`launch_rules.hourly_cap_cents`). Raises `ConfirmationRequired` when the job's estimated data
    transfer could cost a large share of the budget, unless `confirm_transfer_cost`. Given a `registry`, an image it
    refuses is a `ValidationFailed` (`check_image`); the shell submitter (`krater.compute.submit`) passes none."""
    if not _can_launch_for(actor, project):
        raise NotAllowed("Only the project's team can run jobs on its budget.")
    if project.status not in LAUNCHABLE_STATUSES:
        raise InvalidState(f"A {project.status.value} project can't run jobs.")
    _validate(spec, cap_microusd=cap_cents * MICROUSD_PER_CENT)
    report = check_image(registry, spec.image, spec=spec, session=session, now=now)
    spec = replace(spec, min_cuda_major=report.min_cuda_major)
    funding_holds.require_not_held(project)
    clearance.require_team_cleared(session, weave_client, project)
    inputs_bytes = inputs_size(store, project, spec)
    estimate = transfer_estimate(session, project, spec, inputs_bytes=inputs_bytes)
    if estimate is not None and estimate.needs_confirmation and not confirm_transfer_cost:
        raise ConfirmationRequired(estimate.message())
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
        data_in_gb=spec.data_in_gb,
        data_out_gb=spec.data_out_gb,
        input_names=list(spec.inputs),
        inputs_bytes=inputs_bytes,
        smoke_minutes=spec.smoke_minutes,
        checkpoint_upload_minutes=spec.checkpoint_upload_minutes,
        max_spend_cents=spec.max_spend_cents,
        min_cuda_major=spec.min_cuda_major,
        image_bytes=report.size_bytes,
        state=ComputeJobState.QUEUED,
        # While compute is stopped, say so from the start, as the scheduler's next pass would.
        state_reason=f"Waiting: {halted}."
        if (halted := compute_halt.current(session)) is not None
        else "Waiting for an offer at or under its "
        + (
            "limit."
            if spec.on_outbid is ComputeOutbid.HOLD and spec.target_microusd == spec.max_microusd
            else "target price."
        ),
        queued_at=now,
        wait_started_at=now,
    )
    session.add(job)
    session.flush()
    session.add(ComputeEvent(job_id=job.id, kind="queued", message="Queued.", payload={}, at=now))
    for warning in report.warnings:
        session.add(ComputeEvent(job_id=job.id, kind=IMAGE_WARNING, message=warning, payload={}, at=now))
    # Not stored on the job: what the API's submit answers with (the events above keep them).
    job.image_warnings = report.warnings
    if spec.kind is ComputeJobKind.BOX:
        if spec.workspace_id is not None:
            workspaces.attach(
                session, actor, job=job, workspace_id=spec.workspace_id, end_holder=spec.end_holder, now=now
            )
        else:
            workspaces.start_new(session, job, name=spec.workspace_name)
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


def machines_in_use(session: Session, project: Project) -> int:
    """How many of `project`'s machines count against its "machines at once" limit."""
    return session.scalar(
        sa.select(sa.func.count())
        .select_from(ComputeInstance)
        .join(ComputeJob, ComputeJob.id == ComputeInstance.job_id)
        .where(ComputeJob.project_id == project.id, ComputeInstance.state.in_(LIMITED_STATES))
    )


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


def _require_under_spend_cap(job: ComputeJob) -> None:
    """A box at its spend cap would be stopped again at once: say so instead, and how to go on."""
    if job.max_spend_cents is not None and (job.spend_cents or 0) >= job.max_spend_cents:
        raise InvalidState(
            f"This box has reached its spend cap of ${job.max_spend_cents / 100:,.2f}. Raise it under Change limits "
            "(or krater limits) first."
        )


def resume_box(session: Session, actor: Actor, *, job: ComputeJob, now: datetime, weave_client: WeaveClient) -> None:
    """Ask the scheduler to start a stopped box on its machine again (only works while the machine's GPU is free).
    Starting it pays for its GPU again, so the team must be cleared in Weave, as for a new job."""
    _box_for(actor, job)
    row = _live_instance(job)
    if job.state is not ComputeJobState.STOPPED or row is None:
        raise InvalidState("Only a stopped box can be resumed.")
    _require_under_spend_cap(job)
    if job.project.status not in LAUNCHABLE_STATUSES:
        raise InvalidState(f"A {job.project.status.value} project can't run boxes.")
    funding_holds.require_not_held(job.project)
    compute_halt.require_running(session)
    limit = compute_settings.effective_policy(session, job.project).max_machines
    if machines_in_use(session, job.project) >= limit:
        raise InvalidState(
            f"The project already has {limit} machines at once, its limit. Resume this box once one of them ends."
        )
    clearance.require_team_cleared(session, weave_client, job.project)
    row.start_requested_at = now
    job.state_reason = "Resuming on the same machine."
    session.add(
        ComputeEvent(
            job_id=job.id, instance_id=row.id, kind="resume_requested", message=job.state_reason, payload={}, at=now
        )
    )
    session.flush()


#: How an End box the member asked for reads in the stop's reason.
END_REASON = "ending"


def end_box(session: Session, actor: Actor, *, job: ComputeJob, now: datetime) -> None:
    """End a box after saving /workspace ("End box"), by its team or an admin. A running box's runner saves first and
    the scheduler then cancels it and destroys its machine (`Scheduler._end_box`), as a regular stop of all compute
    saves first; a box that can't save now (queued, starting, paused, or stopped, whose stop saved already) ends at
    once. "End now" (`cancel_job`) skips the save, as an emergency stop does, and also cuts short an End box that is
    still saving. Either way the box's workspace stays (`krater.services.workspaces`)."""
    _box_for(actor, job)
    if job.state in FINISHED_JOB_STATES:
        raise InvalidState(f"The box is already {job.state.value}.")
    row = _live_instance(job)
    if job.state is not ComputeJobState.RUNNING or row is None or row.runner_starts == 0:
        cancel_job(session, actor, job=job, now=now)
        job.state_reason = f"Ended by {_who(actor, job)}."
        return
    if job.end_requested_at is None:
        job.end_requested_at = now
        job.state_reason = "Ending: saving /workspace first, then its machine is destroyed."
        session.add(
            ComputeEvent(
                job_id=job.id,
                instance_id=row.id,
                kind="end_requested",
                message=f"Ending, after a save of /workspace: asked by {_who(actor, job)}.",
                payload={"by": str(actor.user.id)},
                at=now,
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
    _require_under_spend_cap(job)
    row.destroy_requested_at = now
    row.destroy_reason = MOVED_BY_MEMBER
    job.state_reason = "Moving: starting elsewhere from the last saved workspace."
    session.add(
        ComputeEvent(
            job_id=job.id, instance_id=row.id, kind="move_requested", message=job.state_reason, payload={}, at=now
        )
    )
    session.flush()


#: The audit action for an admin changing a job's limits (a team member's change is in the job's history only).
AUDIT_JOB_LIMITS = "compute_job_limits"
MAX_RUNTIME_MINUTES = 7 * 24 * 60
#: `change_limits`'s `max_spend_cents` when the request leaves the cap as it is.
UNCHANGED = object()


def _rate_microusd(job: ComputeJob) -> int | None:
    """What the job's current machine costs an hour, if it has one."""
    live = [
        i for i in job.instances if i.state not in (ComputeInstanceState.DESTROYED, ComputeInstanceState.CREATE_FAILED)
    ]
    return live[-1].full_microusd if live else None


def _others_held(session: Session, job: ComputeJob) -> int:
    rows = session.execute(
        sa.select(ComputeJob.hold_cents, ComputeJob.spend_cents).where(
            ComputeJob.project_id == job.project_id,
            ComputeJob.state.in_(ACTIVE_JOB_STATES),
            ComputeJob.id != job.id,
        )
    )
    return sum(max(0, hold - (spent or 0)) for hold, spent in rows)


def change_limits(
    session: Session,
    actor: Actor,
    *,
    job: ComputeJob,
    now: datetime,
    max_runtime_minutes: int | None = None,
    max_spend_cents=UNCHANGED,
) -> list[str]:
    """Change an unfinished job's max runtime and spend cap (None removes the cap), by its team or an admin. Raising
    the runtime of a job with a machine raises its budget hold as far as the project's free budget allows, as when
    it was rented. A box stopped at its cap can be resumed once the cap is raised. Returns what changed, in words."""
    if not (actor.is_admin or _can_launch_for(actor, job.project)):
        raise NotAllowed("Only the project's team or an admin can change this job.")
    if job.state in FINISHED_JOB_STATES:
        raise InvalidState(f"The job is already {job.state.value}.")
    errors: dict[str, str] = {}
    changed: list[str] = []
    if max_runtime_minutes is not None and max_runtime_minutes != job.max_runtime_minutes:
        if job.smoke_minutes:
            errors["max_runtime_minutes"] = "A smoke test stops after its own minutes; rerun it without --smoke."
        elif not 1 <= max_runtime_minutes <= MAX_RUNTIME_MINUTES:
            errors["max_runtime_minutes"] = "Choose a max runtime of 1 minute to 7 days."
        else:
            changed.append(f"max runtime {job.max_runtime_minutes} → {max_runtime_minutes} minutes")
    if max_spend_cents is not UNCHANGED and max_spend_cents != job.max_spend_cents:
        spent = job.spend_cents or 0
        if max_spend_cents is not None and not 1 <= max_spend_cents <= MAX_SPEND_CAP_CENTS:
            errors["max_spend"] = "Give a spend cap of $0.01 to $100,000, or none."
        elif max_spend_cents is not None and max_spend_cents <= spent:
            errors["max_spend"] = f"It has already cost ${spent / 100:,.2f}: give a cap above that, or cancel it."
        else:
            before = "none" if job.max_spend_cents is None else f"${job.max_spend_cents / 100:,.2f}"
            after = "none" if max_spend_cents is None else f"${max_spend_cents / 100:,.2f}"
            changed.append(f"spend cap {before} → {after}")
    if errors:
        raise ValidationFailed(errors)
    if not changed:
        return []
    if max_runtime_minutes is not None:
        job.max_runtime_minutes = max_runtime_minutes
    if max_spend_cents is not UNCHANGED:
        job.max_spend_cents = max_spend_cents
    note = ""
    rate = _rate_microusd(job)
    if rate is not None and max_runtime_minutes is not None:
        wanted = math.ceil(rate * max_runtime_minutes / 60 / MICROUSD_PER_CENT)
        free = budget.remaining_cents(session, job.project) - _others_held(session, job)
        job.hold_cents = max(job.hold_cents, min(wanted, free + (job.spend_cents or 0)))
        if job.hold_cents < wanted:
            note = (
                f" The project's free budget covers about ${job.hold_cents / 100:,.2f} of it; it stops when the "
                "budget runs out."
            )
    who = _who(actor, job)
    message = f"Limits changed by {who}: {'; '.join(changed)}.{note}"
    session.add(
        ComputeEvent(job_id=job.id, kind="limits_changed", message=message, payload={"by": str(actor.user.id)}, at=now)
    )
    if who == "an admin":
        audit.record(
            session,
            actor,
            AUDIT_JOB_LIMITS,
            project=job.project,
            payload={"job_id": str(job.id), "changed": changed},
        )
    session.flush()
    return changed
