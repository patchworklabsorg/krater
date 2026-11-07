"""An admin's SSH access to a running box, from the box's job page: grant (with a reason) and revoke. Admins only,
checked in Weave (`fresh_actor`). The rules are in `krater.services.box_access`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import ComputeJob
from krater.services import box_access
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor
from krater.web.flash import flash
from krater.worker.app import slack_post_admin_override

router = APIRouter()


def _admin_job(db_session: Session, actor: Actor, project_id: uuid.UUID, job_id: uuid.UUID) -> ComputeJob:
    if not get_settings().compute_on:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not actor.is_admin:
        raise NotAllowed("Only an admin can get SSH access to a member's box.")
    job = db_session.get(ComputeJob, job_id)
    if job is None or job.project_id != project_id:
        raise NotFound("No such job.")
    return job


def _back(job: ComputeJob) -> RedirectResponse:
    return RedirectResponse(f"/projects/{job.project_id}/compute/{job.id}#admin-access", status_code=303)


@router.post("/projects/{project_id}/compute/{job_id}/admin-access", dependencies=[Depends(verify_csrf_token)])
def grant_access(
    request: Request,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    reason: Annotated[str, Form()] = "",
):
    job = _admin_job(db_session, actor, project_id, job_id)
    try:
        access = box_access.grant(db_session, actor, job=job, reason=reason, now=datetime.now(UTC))
    except (InvalidState, ValidationFailed) as exc:
        db_session.rollback()
        message = "; ".join(exc.errors.values()) if isinstance(exc, ValidationFailed) else str(exc)
        flash(request, message, "error")
        return _back(job)
    db_session.commit()
    until = access.expires_at.strftime("%H:%M UTC")
    slack_post_admin_override.defer(
        project_id=str(job.project_id),
        action="admin_box_access",
        actor_sub=actor.user.weave_sub,
        reason=access.reason,
        extra=f"SSH access to the box “{job.name}” until {until}.",
    )
    flash(
        request,
        f"Your SSH keys reach the box within a minute, until {until}. The team can see that you have access.",
        "success",
    )
    return _back(job)


@router.post(
    "/projects/{project_id}/compute/{job_id}/admin-access/{access_id}/revoke",
    dependencies=[Depends(verify_csrf_token)],
)
def revoke_access(
    request: Request,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    access_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    job = _admin_job(db_session, actor, project_id, job_id)
    try:
        box_access.revoke(db_session, actor, access_id=access_id, job=job, now=datetime.now(UTC))
    except InvalidState as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return _back(job)
    db_session.commit()
    flash(
        request,
        "Access ended. The keys leave the box within a minute; a session already open stays open until it closes.",
        "success",
    )
    return _back(job)
