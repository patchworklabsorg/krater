"""The admin overview: `/admin`, admins only -- every project grouped by status, a read-only table of
`ApprovalPolicy` rows, and delivery to Quilt (events Quilt refused, with Retry and Dismiss)."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import ApprovalPolicy, Project, ProjectStatus
from krater.services import projects as project_service
from krater.services import quilt_events
from krater.services.actor import Actor
from krater.services.errors import InvalidState, ValidationFailed
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor, require_admin
from krater.web.flash import flash
from krater.web.templates import templates
from krater.worker.app import kick_quilt_delivery

router = APIRouter()


@router.get("/admin")
def admin_overview(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    all_projects = list(db_session.scalars(sa.select(Project).order_by(Project.created_at.desc())))
    summaries = [project_service.project_summary(db_session, project=project) for project in all_projects]

    by_status: dict[ProjectStatus, list] = {status: [] for status in ProjectStatus}
    for summary in summaries:
        by_status[summary.status].append(summary)

    policies = list(
        db_session.scalars(sa.select(ApprovalPolicy).order_by(ApprovalPolicy.stage, ApprovalPolicy.min_budget_cents))
    )

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "by_status": by_status,
            "policies": policies,
            "statuses": list(ProjectStatus),
            "quilt": quilt_events.delivery_overview(db_session, actor),
        },
    )


def _quilt_action(db_session: Session, request: Request, action, actor: Actor, event_id: uuid.UUID, reason: str):
    try:
        action(db_session, actor, row_id=event_id, reason=reason)
    except (InvalidState, ValidationFailed) as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/admin#quilt", status_code=303)
    db_session.commit()
    kick_quilt_delivery()
    flash(request, "Quilt event updated.", "success")
    return RedirectResponse("/admin#quilt", status_code=303)


@router.post("/admin/quilt/{event_id}/retry", dependencies=[Depends(verify_csrf_token)])
def quilt_retry(
    request: Request,
    event_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    reason: Annotated[str, Form()] = "",
):
    return _quilt_action(db_session, request, quilt_events.admin_retry, actor, event_id, reason)


@router.post("/admin/quilt/{event_id}/dismiss", dependencies=[Depends(verify_csrf_token)])
def quilt_dismiss(
    request: Request,
    event_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    reason: Annotated[str, Form()] = "",
):
    return _quilt_action(db_session, request, quilt_events.admin_dismiss, actor, event_id, reason)


__all__ = ["router"]
