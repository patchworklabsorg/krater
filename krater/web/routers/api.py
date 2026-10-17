"""Krater's API for the `krater` command (`cli/`, docs/compute/cli.md): a member's projects, jobs, boxes, secrets and
prices, as JSON.

Every route but `cli-config` takes only `Authorization: Bearer <token>`: a Weave access token the CLI got for its own
public Weave app (`KRATER_WEAVE_CLI_CLIENT_ID`), checked with Weave on each request (`WeaveClient.cli_token_sub`,
cached for a minute). Never the session cookie, so these routes need no CSRF token and a page can't call them on a
member's behalf. The user is then authorized like any page: a Weave directory lookup by `sub`, fresh for anything
that changes state. Errors are JSON `{"error": ...}`. Only with compute on (`KRATER_COMPUTE_MODE=vast`) and the CLI's
app configured; otherwise every route is a 404.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import ComputeEscalation, ComputeEvent, ComputeJob, ComputeJobKind, ComputeOutbid, Project, User
from krater.services import compute_files, compute_jobs, price_history, project_secrets, teams
from krater.services import projects as project_service
from krater.services import users as user_service
from krater.services.actor import Actor
from krater.services.compute_jobs import JobSpec
from krater.services.errors import DomainError, InvalidState, NotAllowed, NotAMember, NotFound, ValidationFailed
from krater.services.launch_rules import LAUNCHABLE_STATUSES, hourly_cap_cents
from krater.storage import StorageError, get_compute_store
from krater.vast import microusd_to_usd, usd_to_microusd
from krater.weave import WeaveClient, WeaveUnavailableError, get_weave_client

router = APIRouter(prefix="/api/v1")

#: Doorkeeper's out-of-band redirect: Weave shows the code and the member pastes it into the CLI.
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
CLI_SCOPES = "openid profile"


def _enabled() -> None:
    settings = get_settings()
    if not settings.compute_on or not settings.weave_cli_client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _error(status_code: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status_code)


def api_actor(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
) -> Actor:
    _enabled()
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with `krater login`.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        sub = weave_client.cli_token_sub(token.strip())
    except WeaveUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Weave is unavailable") from exc
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your sign-in has expired or was revoked. Run `krater login`.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    user = db_session.scalar(sa.select(User).where(User.weave_sub == sub))
    if user is None:
        # Krater creates its user record at the first browser sign-in, from Weave's id_token. Ask for that once
        # rather than build a second way to create accounts.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sign in to Krater in a browser once, then try again."
        )
    try:
        return user_service.authorize(db_session, weave_client, user, fresh=request.method not in ("GET", "HEAD"))
    except NotAMember as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except WeaveUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Weave is unavailable") from exc


Me = Annotated[Actor, Depends(api_actor)]
Db = Annotated[Session, Depends(get_session)]


def _is_team(actor: Actor, project: Project) -> bool:
    return teams.on_team(project, actor.user.id)


def _project(session: Session, actor: Actor, project_id: uuid.UUID) -> Project:
    try:
        project = project_service.get_project(session, project_id=project_id)
    except NotFound:
        project = None
    if project is None or not (_is_team(actor, project) or actor.is_reviewer or actor.is_admin):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No project {project_id}.")
    return project


def _job(session: Session, actor: Actor, job_id: uuid.UUID) -> ComputeJob:
    job = session.get(ComputeJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No job {job_id}.")
    _project(session, actor, job.project_id)
    return job


def _rate(value: int | None) -> str | None:
    return None if value is None else str(microusd_to_usd(value).quantize(Decimal("0.0001")))


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _job_json(job: ComputeJob, *, detail: bool = False, session: Session | None = None) -> dict:
    data: dict[str, Any] = {
        "id": str(job.id),
        "project_id": str(job.project_id),
        "name": job.name,
        "kind": job.kind.value,
        "state": job.state.value,
        "state_reason": job.state_reason,
        "image": job.image,
        "gpu_names": list(job.gpu_names),
        "num_gpus": job.num_gpus,
        "disk_gb": job.disk_gb,
        "target_usd_per_hour": _rate(job.target_microusd),
        "max_usd_per_hour": _rate(job.max_microusd),
        "limit_order": job.is_limit_order,
        "on_outbid": job.on_outbid.value,
        "interruptible": job.interruptible,
        "spend_cents": job.spend_cents,
        "exit_code": job.exit_code,
        "queued_at": _iso(job.queued_at),
        "started_at": _iso(job.started_at),
        "ended_at": _iso(job.ended_at),
    }
    if detail:
        live = [row for row in job.instances if row.is_live]
        row = live[-1] if live else None
        data["command"] = job.command
        data["max_runtime_minutes"] = job.max_runtime_minutes
        data["machine"] = (
            None
            if row is None
            else {
                "gpu": row.gpu_name,
                "usd_per_hour": _rate(row.gpu_microusd + row.disk_microusd),
                "state": row.state.value,
                "ssh_host": row.ssh_host,
                "ssh_port": row.ssh_port,
            }
        )
        events = session.scalars(
            sa.select(ComputeEvent).where(ComputeEvent.job_id == job.id).order_by(ComputeEvent.at.desc()).limit(50)
        )
        data["events"] = [{"at": _iso(e.at), "kind": e.kind, "message": e.message} for e in events]
    return data


@router.get("/cli-config")
def cli_config():
    """What the CLI needs to sign in: Weave's issuer and the CLI's app. Public: nothing here is secret."""
    _enabled()
    settings = get_settings()
    return {
        "issuer": settings.weave_issuer.rstrip("/"),
        "client_id": settings.weave_cli_client_id,
        "scopes": CLI_SCOPES,
        "redirect_uri": OOB_REDIRECT_URI,
        "loopback": settings.weave_cli_loopback,
    }


@router.get("/me")
def me(actor: Me):
    return {
        "name": actor.display_name,
        "email": actor.email,
        "member": actor.is_member,
        "reviewer": actor.is_reviewer,
        "admin": actor.is_admin,
    }


@router.get("/projects")
def my_projects(actor: Me, db_session: Db):
    """The projects the member is on the team of (lead or accepted member)."""
    projects = [
        p for p in db_session.scalars(sa.select(Project).order_by(Project.created_at.desc())) if _is_team(actor, p)
    ]
    return {
        "projects": [
            {
                "id": str(p.id),
                "title": p.title,
                "status": p.status.value,
                "can_launch": p.status in LAUNCHABLE_STATUSES,
                "hourly_cap_cents": hourly_cap_cents(p, get_settings()),
            }
            for p in projects
        ]
    }


@router.get("/projects/{project_id}/jobs")
def project_jobs(project_id: uuid.UUID, actor: Me, db_session: Db):
    project = _project(db_session, actor, project_id)
    jobs = db_session.scalars(
        sa.select(ComputeJob).where(ComputeJob.project_id == project.id).order_by(ComputeJob.created_at.desc())
    )
    return {"jobs": [_job_json(job) for job in jobs]}


def _price(body: dict, field: str) -> int:
    try:
        value = Decimal(str(body[field]))
    except (KeyError, InvalidOperation):
        raise ValidationFailed({field: "Give a price in dollars per hour, such as 0.12."}) from None
    if not value.is_finite() or not Decimal(0) < value <= Decimal(1000):
        raise ValidationFailed({field: "Give a price above zero, in dollars per hour."})
    return usd_to_microusd(value)


def _choice[E: enum.StrEnum](body: dict, field: str, kind: type[E], default: str) -> E:
    try:
        return kind(body.get(field, default))
    except ValueError:
        choices = ", ".join(member.value for member in kind)
        raise ValidationFailed({field: f"Choose one of: {choices}."}) from None


def _int(body: dict, field: str, default: int | None) -> int | None:
    value = body.get(field, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed({field: "Give a whole number."})
    return value


@router.post("/projects/{project_id}/jobs", status_code=201)
async def submit(project_id: uuid.UUID, request: Request, actor: Me, db_session: Db):
    project = _project(db_session, actor, project_id)
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Send a JSON object.")
    if not isinstance(body, dict):
        return _error(400, "Send a JSON object.")
    try:
        gpus = body.get("gpu_names", [])
        if not isinstance(gpus, list) or not all(isinstance(g, str) and len(g) <= 100 for g in gpus) or len(gpus) > 50:
            raise ValidationFailed({"gpu_names": "Give a list of GPU model names."})
        command = body.get("command")
        if command is not None and not isinstance(command, str):
            raise ValidationFailed({"command": "Give the command as one string."})
        if "limit_usd_per_hour" in body:
            # A limit order: one price it starts at or under and never pays more than, and it holds its machine
            # when outbid rather than starting over elsewhere.
            if "target_usd_per_hour" in body or "max_usd_per_hour" in body:
                raise ValidationFailed({"limit_usd_per_hour": "Give a limit, or a target and a max, not both."})
            target = high = _price(body, "limit_usd_per_hour")
            outbid_default = ComputeOutbid.HOLD.value
        else:
            target, high = _price(body, "target_usd_per_hour"), _price(body, "max_usd_per_hour")
            outbid_default = ComputeOutbid.MOVE.value
        spec = JobSpec(
            name=str(body.get("name", "")),
            image=str(body.get("image", "")).strip(),
            command=command or None,
            kind=ComputeJobKind.BOX if body.get("kind") == "box" else ComputeJobKind.JOB,
            gpu_names=tuple(gpus),
            min_gpu_ram_gb=_int(body, "min_gpu_ram_gb", None),
            num_gpus=_int(body, "num_gpus", 1),
            disk_gb=_int(body, "disk_gb", 32),
            target_microusd=target,
            max_microusd=high,
            interruptible=bool(body.get("interruptible", True)),
            max_wait_minutes=_int(body, "max_wait_minutes", 12 * 60),
            on_max_wait=_choice(body, "on_max_wait", ComputeEscalation, "keep_waiting"),
            max_runtime_minutes=_int(body, "max_runtime_minutes", 8 * 60),
            on_outbid=_choice(body, "on_outbid", ComputeOutbid, outbid_default),
        )
        job = compute_jobs.submit_job(
            db_session,
            actor,
            project=project,
            spec=spec,
            cap_cents=hourly_cap_cents(project, get_settings()),
            now=datetime.now(UTC),
        )
    except ValidationFailed as exc:
        db_session.rollback()
        return _error(422, "Check the job's fields.", fields=exc.errors)
    except NotAllowed as exc:
        db_session.rollback()
        return _error(403, str(exc))
    except (InvalidState, DomainError) as exc:
        db_session.rollback()
        return _error(409, str(exc))
    db_session.commit()
    return _job_json(job)


@router.get("/jobs/{job_id}")
def job_detail(job_id: uuid.UUID, actor: Me, db_session: Db):
    return _job_json(_job(db_session, actor, job_id), detail=True, session=db_session)


@router.get("/jobs/{job_id}/logs")
def job_logs(job_id: uuid.UUID, actor: Me, db_session: Db, attempt: int | None = None, bytes: int = 65536):
    """The end of the job's output log (`bytes` of it, at most 1 MB): the latest attempt's, or `attempt`'s."""
    job = _job(db_session, actor, job_id)
    try:
        tail = compute_files.log_tail(
            get_compute_store(), actor, job=job, attempt=attempt, max_bytes=max(1, min(bytes, 1024 * 1024))
        )
    except NotAllowed as exc:
        return _error(403, str(exc))
    except StorageError:
        return _error(503, "Krater's storage didn't answer; try again in a minute.")
    if tail is None:
        return {"attempt": attempt, "text": "", "size_bytes": 0, "truncated": False}
    return {"attempt": tail.attempt, "text": tail.text, "size_bytes": tail.size_bytes, "truncated": tail.truncated}


@router.get("/jobs/{job_id}/files")
def job_files(job_id: uuid.UUID, actor: Me, db_session: Db):
    """The job's outputs, checkpoints and logs, each with a download URL that works for 15 minutes."""
    job = _job(db_session, actor, job_id)
    store = get_compute_store()
    try:
        downloads = compute_files.list_downloads(store, actor, job=job)
        return {
            "files": [{"area": f.area, "name": f.name, "size_bytes": f.size_bytes, "url": url} for f, url in downloads]
        }
    except NotAllowed as exc:
        return _error(403, str(exc))
    except StorageError:
        return _error(503, "Krater's storage didn't answer; try again in a minute.")


_ACTIONS = {
    "cancel": compute_jobs.cancel_job,
    "stop": compute_jobs.stop_box,
    "resume": compute_jobs.resume_box,
    "move": compute_jobs.move_box,
}


@router.post("/jobs/{job_id}/{action}")
def job_action(job_id: uuid.UUID, action: str, actor: Me, db_session: Db):
    if action not in _ACTIONS:
        return _error(404, f"No action {action!r}.")
    job = _job(db_session, actor, job_id)
    try:
        _ACTIONS[action](db_session, actor, job=job, now=datetime.now(UTC))
    except NotAllowed as exc:
        db_session.rollback()
        return _error(403, str(exc))
    except (InvalidState, DomainError) as exc:
        db_session.rollback()
        return _error(409, str(exc))
    db_session.commit()
    return _job_json(job)


@router.get("/projects/{project_id}/secrets")
def list_secrets(project_id: uuid.UUID, actor: Me, db_session: Db):
    project = _project(db_session, actor, project_id)
    return {
        "enabled": project_secrets.enabled(get_settings()),
        "secrets": [
            {"name": s.name, "updated_at": _iso(s.updated_at)}
            for s in project_secrets.list_secrets(db_session, project)
        ],
    }


@router.put("/projects/{project_id}/secrets/{name}")
async def set_secret(project_id: uuid.UUID, name: str, request: Request, actor: Me, db_session: Db):
    project = _project(db_session, actor, project_id)
    try:
        body = await request.json()
    except ValueError:
        return _error(400, 'Send {"value": "..."}.')
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str):
        return _error(400, 'Send {"value": "..."}.')
    try:
        secret = project_secrets.set_secret(
            db_session, actor, project=project, name=name, value=value, settings=get_settings()
        )
    except ValidationFailed as exc:
        db_session.rollback()
        return _error(422, "Check the secret.", fields=exc.errors)
    except NotAllowed as exc:
        db_session.rollback()
        return _error(403, str(exc))
    except InvalidState as exc:
        db_session.rollback()
        return _error(409, str(exc))
    db_session.commit()
    return {"name": secret.name, "updated_at": _iso(secret.updated_at)}


@router.delete("/projects/{project_id}/secrets/{name}")
def delete_secret(project_id: uuid.UUID, name: str, actor: Me, db_session: Db):
    project = _project(db_session, actor, project_id)
    try:
        project_secrets.delete_secret(db_session, actor, project=project, name=name)
    except NotAllowed as exc:
        db_session.rollback()
        return _error(403, str(exc))
    except NotFound as exc:
        db_session.rollback()
        return _error(404, str(exc))
    db_session.commit()
    return {"deleted": name}


@router.get("/prices")
def prices(actor: Me, db_session: Db):
    """The newest sample of every GPU model Krater tracks: what a full hour costs now (32 GB disk included)."""
    del actor
    return {
        "prices": [
            {
                "gpu": s.gpu_name,
                "rental": s.rental_type,
                "sampled_at": _iso(s.sampled_at),
                "offers": s.offer_count,
                "min_usd_per_hour": _rate(s.min_microusd),
                "median_usd_per_hour": _rate(s.median_microusd),
            }
            for s in price_history.latest(db_session)
        ]
    }


__all__ = ["CLI_SCOPES", "OOB_REDIRECT_URI", "api_actor", "router"]
