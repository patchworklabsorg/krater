"""Krater Compute for members: a project's jobs and boxes, the submit form, one job's page, and the GPU price history.

Only with `KRATER_COMPUTE_MODE=vast`; otherwise these pages don't exist. The portal never talks to Vast: it writes
jobs and requests, and the worker's scheduler acts on them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import (
    ComputeEscalation,
    ComputeEvent,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    ComputeOutbid,
    Project,
)
from krater.models.compute_job import ACTIVE_JOB_STATES
from krater.registry import RegistryError, parse_reference
from krater.services import (
    box_access,
    clearance,
    compute_files,
    compute_jobs,
    compute_settings,
    people,
    price_history,
    project_secrets,
    ssh_keys,
    teams,
)
from krater.services import projects as project_service
from krater.services.actor import Actor
from krater.services.compute_jobs import JobSpec
from krater.services.errors import ConfirmationRequired, InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.services.launch_rules import LAUNCHABLE_STATUSES, hourly_cap_cents
from krater.storage import StorageError, get_compute_store
from krater.vast import usd_to_microusd
from krater.weave import WeaveClient, get_weave_client
from krater.weave import links as weave_links
from krater.web.charts import PricePoint, price_chart_svg
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor, session_actor
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()

ESCALATION_LABELS = {
    ComputeEscalation.KEEP_WAITING: "Keep waiting",
    ComputeEscalation.RAISE_BID: "Accept offers up to my max price",
    ComputeEscalation.ON_DEMAND: "Rent on demand, up to my max price",
    ComputeEscalation.CANCEL: "Cancel the job",
}


def _require_compute() -> None:
    if not get_settings().compute_on:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _is_team(actor: Actor, project: Project) -> bool:
    return teams.on_team(project, actor.user.id)


def _project(session: Session, actor: Actor, project_id: uuid.UUID) -> Project:
    project = project_service.get_project(session, project_id=project_id)
    if not (_is_team(actor, project) or actor.is_reviewer or actor.is_admin):
        raise NotFound(f"No project with id {project_id}.")
    return project


def _pinned_reference(image: str, digest: str | None) -> str | None:
    """`image` at exactly `digest`, for the admin's Inspect button; None without a digest."""
    if digest is None:
        return None
    try:
        return parse_reference(image).pinned(digest)
    except (RegistryError, ValueError):
        return None


def _job(session: Session, project: Project, job_id: uuid.UUID) -> ComputeJob:
    job = session.get(ComputeJob, job_id)
    if job is None or job.project_id != project.id:
        raise NotFound("No such job.")
    return job


def _rate(raw: str, field: str) -> int:
    try:
        value = Decimal(raw.strip().lstrip("$"))
    except InvalidOperation:
        raise ValidationFailed({field: "Enter a price in dollars per hour, such as 0.12."}) from None
    if not Decimal(0) < value <= Decimal(1000):
        raise ValidationFailed({field: "Enter a price above zero, in dollars per hour."})
    return usd_to_microusd(value)


def _int(raw: str, field: str, label: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValidationFailed({field: f"Enter a whole number for {label}."}) from None


@router.get("/projects/{project_id}/compute")
def compute_index(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(session_actor)],
):
    _require_compute()
    project = _project(db_session, actor, project_id)
    return _index_page(request, db_session, actor, project)


def _index_page(request: Request, db_session: Session, actor: Actor, project: Project, *, errors=None, posted=None):
    jobs = list(
        db_session.scalars(
            sa.select(ComputeJob).where(ComputeJob.project_id == project.id).order_by(ComputeJob.created_at.desc())
        )
    )
    return templates.TemplateResponse(
        request,
        "compute/index.html",
        {
            "project": project,
            "jobs": jobs,
            "active_states": ACTIVE_JOB_STATES,
            "can_launch": _is_team(actor, project)
            and project.status in LAUNCHABLE_STATUSES
            and project.funding_hold is None,  # held: launching is refused (`funding_holds`)
            "secrets": project_secrets.list_secrets(db_session, project),
            "secrets_enabled": project_secrets.enabled(get_settings()),
            "can_manage_secrets": actor.is_member and (_is_team(actor, project) or actor.is_admin),
            "max_secrets": project_secrets.MAX_SECRETS_PER_PROJECT,
            "errors": errors or {},
            "posted": posted or {},
        },
        status_code=422 if errors else 200,
    )


@router.post("/projects/{project_id}/compute/secrets", dependencies=[Depends(verify_csrf_token)])
def set_secret(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    name: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
):
    _require_compute()
    project = _project(db_session, actor, project_id)
    try:
        secret = project_secrets.set_secret(
            db_session, actor, project=project, name=name, value=value, settings=get_settings()
        )
    except ValidationFailed as exc:
        db_session.rollback()
        # Never echo the value back into the page.
        return _index_page(request, db_session, actor, project, errors=exc.errors, posted={"name": name})
    except InvalidState as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/projects/{project.id}/compute", status_code=303)
    db_session.commit()
    flash(request, f"Saved {secret.name}. Jobs get it when they next start.", "success")
    return RedirectResponse(f"/projects/{project.id}/compute", status_code=303)


@router.post("/projects/{project_id}/compute/secrets/{name}/delete", dependencies=[Depends(verify_csrf_token)])
def delete_secret(
    request: Request,
    project_id: uuid.UUID,
    name: str,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    _require_compute()
    project = _project(db_session, actor, project_id)
    project_secrets.delete_secret(db_session, actor, project=project, name=name)
    db_session.commit()
    flash(request, f"Deleted {name}. Running machines keep the value they started with.", "success")
    return RedirectResponse(f"/projects/{project.id}/compute", status_code=303)


def _form_page(
    request: Request,
    db_session: Session,
    project: Project,
    weave_client: WeaveClient,
    *,
    errors=None,
    posted=None,
    status_code=200,
    transfer_warning: str | None = None,
):
    settings = get_settings()
    policy = compute_settings.effective_policy(db_session, project)
    return templates.TemplateResponse(
        request,
        "compute/new.html",
        {
            "project": project,
            "gpus": settings.price_history_gpus,
            "latest": price_history.latest(db_session),
            "cap_cents": hourly_cap_cents(project, settings),
            "transfer_cap_per_tb": compute_settings.format_value(
                compute_settings.FIELDS_BY_NAME["transfer_cap_microusd_per_gb"], policy.transfer_cap_microusd_per_gb
            ),
            "transfer_warning": transfer_warning,
            "escalations": ESCALATION_LABELS,
            "errors": errors or {},
            "posted": posted or {},
            # Compute is held until Weave clears every team member (`krater.services.clearance`); the page says who
            # first, and the submit checks Weave fresh. `None`: Weave couldn't say right now.
            "verification_pending": clearance.pending_for_display(db_session, weave_client, project),
            "weave_verification_url": weave_links.verification_url(),
        },
        status_code=status_code,
    )


@router.get("/projects/{project_id}/compute/new")
def new_job(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(session_actor)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    _require_compute()
    project = _project(db_session, actor, project_id)
    if not _is_team(actor, project):
        raise NotAllowed("Only the project's team can run jobs on its budget.")
    return _form_page(request, db_session, project, weave_client)


@router.post("/projects/{project_id}/compute", dependencies=[Depends(verify_csrf_token)])
async def submit_job(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    _require_compute()
    project = _project(db_session, actor, project_id)
    form = await request.form()
    posted = {key: str(value) for key, value in form.items() if key != "csrf_token"}
    posted["gpu"] = [str(g) for g in form.getlist("gpu")]
    try:
        kind = ComputeJobKind.BOX if posted.get("kind") == "box" else ComputeJobKind.JOB
        min_vram = posted.get("min_vram", "").strip()
        data_in, data_out = posted.get("data_in_gb", "").strip(), posted.get("data_out_gb", "").strip()
        if posted.get("order") == "limit":
            target = high = _rate(posted.get("limit", ""), "limit")
            on_outbid = ComputeOutbid.HOLD
        else:
            target, high = _rate(posted.get("target", ""), "target"), _rate(posted.get("max", ""), "max")
            on_outbid = ComputeOutbid.MOVE
        spec = JobSpec(
            name=posted.get("name", ""),
            image=posted.get("image", "").strip(),
            command=posted.get("command") or None,
            kind=kind,
            gpu_names=tuple(posted["gpu"]),
            min_gpu_ram_gb=_int(min_vram, "min_gpu_ram_gb", "GPU memory") if min_vram else None,
            num_gpus=_int(posted.get("num_gpus", "1"), "num_gpus", "GPUs"),
            disk_gb=_int(posted.get("disk_gb", "32"), "disk_gb", "disk"),
            target_microusd=target,
            max_microusd=high,
            interruptible=posted.get("interruptible") == "on",
            max_wait_minutes=_int(posted.get("max_wait_minutes", "720"), "max_wait_minutes", "max wait"),
            on_max_wait=ComputeEscalation(posted.get("on_max_wait", "keep_waiting")),
            max_runtime_minutes=_int(posted.get("max_runtime_minutes", "480"), "max_runtime_minutes", "max runtime"),
            on_outbid=on_outbid,
            data_in_gb=_int(data_in, "data_in_gb", "data into the machine") if data_in else None,
            data_out_gb=_int(data_out, "data_out_gb", "data out of the machine") if data_out else None,
        )
        job = compute_jobs.submit_job(
            db_session,
            actor,
            project=project,
            spec=spec,
            cap_cents=hourly_cap_cents(project, get_settings()),
            now=datetime.now(UTC),
            weave_client=weave_client,
            confirm_transfer_cost=posted.get("confirm_transfer_cost") == "on",
        )
    except ConfirmationRequired as exc:
        db_session.rollback()
        return _form_page(request, db_session, project, weave_client, posted=posted, transfer_warning=str(exc))
    except (ValidationFailed, ValueError) as exc:
        db_session.rollback()
        errors = (
            exc.errors if isinstance(exc, ValidationFailed) else {"on_max_wait": "Choose what happens after the wait."}
        )
        if posted.get("order") == "limit":
            # A limit order's one price is both the target and the max: show their errors on it.
            for field in ("target", "max"):
                if field in errors:
                    errors.setdefault("limit", errors.pop(field))
        return _form_page(request, db_session, project, weave_client, errors=errors, posted=posted, status_code=422)
    except InvalidState as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/projects/{project.id}/compute", status_code=303)
    db_session.commit()
    flash(request, "Queued. It starts when an offer at or under your price appears.", "success")
    return RedirectResponse(f"/projects/{project.id}/compute/{job.id}", status_code=303)


@router.get("/projects/{project_id}/compute/{job_id}")
def job_page(
    request: Request,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],  # it shows or signs the job's files
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    _require_compute()
    project = _project(db_session, actor, project_id)
    job = _job(db_session, project, job_id)
    now = datetime.now(UTC)
    is_box = job.kind is ComputeJobKind.BOX
    box_admins = box_access.admins(db_session, now=now) if is_box else []
    box_admin_names = sorted(people.names(weave_client, box_admins).values(), key=str.casefold)
    events = list(
        db_session.scalars(
            sa.select(ComputeEvent).where(ComputeEvent.job_id == job.id).order_by(ComputeEvent.at.desc()).limit(100)
        )
    )
    live = [row for row in job.instances if row.is_live]
    charged = [row.transfer_charged_microusd for row in job.instances if row.transfer_charged_microusd is not None]
    is_team = _is_team(actor, project)
    can_read_files = compute_files.can_read(actor, project)
    files, log, files_error = [], None, False
    if can_read_files:
        store = get_compute_store()
        try:
            files = compute_files.list_files(store, actor, job=job)
            log = compute_files.log_tail(store, actor, job=job)
        except StorageError:
            files_error = True
    return templates.TemplateResponse(
        request,
        "compute/job.html",
        {
            "project": project,
            "job": job,
            "events": events,
            "instance": live[-1] if live else None,
            "transfer_charged_cents": -(-sum(charged) // compute_jobs.MICROUSD_PER_CENT) if charged else None,
            "can_cancel": (is_team or actor.is_admin) and (job.is_active or job.state is ComputeJobState.QUEUED),
            "can_stop": is_team and job.kind is ComputeJobKind.BOX and job.state is ComputeJobState.RUNNING,
            "can_resume": is_team and job.kind is ComputeJobKind.BOX and job.state is ComputeJobState.STOPPED,
            "escalations": ESCALATION_LABELS,
            "can_read_files": can_read_files,
            "files": files,
            "log": log,
            "files_error": files_error,
            "box_admin_names": box_admin_names,
            "admin_on_box": actor.is_admin and any(user.id == actor.user.id for user in box_admins),
            "access_runner_ok": box_access.runner_takes_key_updates(live[-1] if live else None),
            "admin_has_keys": actor.is_admin and bool(ssh_keys.authorized_keys_for(db_session, actor.user.id)),
            "is_admin": actor.is_admin,
            "image_reference": _pinned_reference(job.image, live[-1].image_digest if live else None),
        },
    )


@router.get("/projects/{project_id}/compute/{job_id}/files/{area}/{name:path}")
def job_file(
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    area: str,
    name: str,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],  # it shows or signs the job's files
):
    """Download one of the job's files: a redirect to a URL signed for 15 minutes, made on the click."""
    _require_compute()
    project = _project(db_session, actor, project_id)
    job = _job(db_session, project, job_id)
    url = compute_files.download_url(get_compute_store(), actor, job=job, area=area, name=name)
    return RedirectResponse(url, status_code=303)


_ACTIONS = {
    "cancel": (compute_jobs.cancel_job, "Cancelled. Its machine, if any, is destroyed within a minute."),
    "stop": (compute_jobs.stop_box, "Stopping: the box saves /workspace first."),
    "resume": (compute_jobs.resume_box, "Resuming on the same machine."),
    "move": (compute_jobs.move_box, "Starting it elsewhere from its last saved workspace."),
}


@router.post("/projects/{project_id}/compute/{job_id}/{action}", dependencies=[Depends(verify_csrf_token)])
def job_action(
    request: Request,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    action: str,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    _require_compute()
    if action not in _ACTIONS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    project = _project(db_session, actor, project_id)
    job = _job(db_session, project, job_id)
    service, message = _ACTIONS[action]
    try:
        # Resuming starts paying for the GPU again, so it checks the team's verification in Weave.
        extra = {"weave_client": weave_client} if action == "resume" else {}
        service(db_session, actor, job=job, now=datetime.now(UTC), **extra)
    except InvalidState as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
    else:
        db_session.commit()
        flash(request, message, "success")
    return RedirectResponse(f"/projects/{project.id}/compute/{job.id}", status_code=303)


@router.get("/pricing/history")
def price_history_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    gpu: str = "",
    rental: str = "bid",
    days: int = 7,
):
    settings = get_settings()
    gpus = settings.price_history_gpus
    gpu = gpu if gpu in gpus else (gpus[0] if gpus else "")
    rental_type = "ondemand" if rental == "ondemand" else "bid"
    days = days if days in (1, 7, 30) else 7
    samples = price_history.series(
        db_session, gpu_name=gpu, rental_type=rental_type, since=datetime.now(UTC) - timedelta(days=days)
    )
    points = [
        PricePoint(s.sampled_at, s.min_microusd, s.p10_microusd, s.median_microusd)
        for s in samples
        if s.min_microusd is not None and s.p10_microusd is not None and s.median_microusd is not None
    ]
    kind = "bid" if rental_type == "bid" else "on-demand"
    return templates.TemplateResponse(
        request,
        "pricing/history.html",
        {
            "gpus": gpus,
            "gpu": gpu,
            "rental": rental_type,
            "days": days,
            "points": points,
            "disk_gb": settings.price_history_disk_gb,
            "chart": price_chart_svg(points, title=f"{gpu} {kind} price per hour, last {days} days"),
        },
    )


__all__ = ["router"]
