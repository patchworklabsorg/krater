"""The admin overview: `/admin`, admins only -- the budget grants held for the team's verification in Weave, every
project grouped by status, a read-only table of `ApprovalPolicy` rows, how the public gallery names members under 18,
and delivery to Quilt (events Quilt refused, with Retry and Dismiss)."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import ApprovalPolicy, Project, ProjectStatus
from krater.services import clearance, grants, people, quilt_events, site_settings
from krater.services import projects as project_service
from krater.services.actor import Actor
from krater.services.errors import InvalidState, ValidationFailed
from krater.weave import WeaveClient, get_weave_client
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor, require_admin
from krater.web.flash import flash
from krater.web.routers import admin_activity
from krater.web.templates import templates
from krater.worker.app import kick_quilt_delivery

router = APIRouter()


@router.get("/admin")
def admin_overview(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    q: str = "",
    status: str = "",
):
    held = _held_grants(db_session, weave_client)
    all_projects = list(db_session.scalars(sa.select(Project).order_by(Project.created_at.desc())))
    summaries = [project_service.project_summary(db_session, project=project) for project in all_projects]

    # The counts are of every project; the table shows the ones the search and status filter keep.
    counts = {each: 0 for each in ProjectStatus}
    for summary in summaries:
        counts[summary.status] += 1
    query = q.strip()
    wanted = next((each for each in ProjectStatus if each.value == status), None)
    by_status: dict[ProjectStatus, list] = {each: [] for each in ProjectStatus}
    for summary in summaries:
        if (wanted is None or summary.status is wanted) and _matches(summary.project, query):
            by_status[summary.status].append(summary)

    policies = list(
        db_session.scalars(sa.select(ApprovalPolicy).order_by(ApprovalPolicy.stage, ApprovalPolicy.min_budget_cents))
    )

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "held": held,
            "recent": admin_activity.recent_activity(db_session, weave_client),
            "by_status": by_status,
            "counts": counts,
            "query": query,
            "status_filter": wanted.value if wanted else "",
            "filtered": bool(query) or wanted is not None,
            "policies": policies,
            "statuses": list(ProjectStatus),
            "quilt": quilt_events.delivery_overview(db_session, actor),
            "gallery_minor_names": site_settings.current(db_session).gallery_minor_names.value,
            "gallery_choices": [
                (choice.value, label) for choice, label in site_settings.GALLERY_MINOR_NAMES_LABELS.items()
            ],
        },
    )


def _matches(project: Project, query: str) -> bool:
    """Whether a project's title, or its lead's Patchwork ID, contains `query` (case-insensitive). Empty matches all.
    Krater stores no names, so a person is found by Patchwork ID."""
    if not query:
        return True
    needle = query.casefold()
    return needle in (project.title or "").casefold() or needle in project.submitter.weave_sub.casefold()


def _held_grants(db_session: Session, weave_client: WeaveClient) -> list[dict]:
    """Each held grant with who decided it and whose verification it waits on now (`krater.services.grants`). The
    lookups may use the directory cache; `waiting` is `None` when Weave can't say, and the page still loads."""
    pending = grants.all_pending(db_session)
    approvers = people.names(weave_client, {held.actor for held in pending})
    return [
        {
            "held": held,
            "kind": grants.KIND_LABELS.get(held.kind, held.kind.value),
            "approver": approvers[held.actor_id],
            "waiting": clearance.pending_for_display(db_session, weave_client, held.project),
        }
        for held in pending
    ]


@router.post("/admin/gallery", dependencies=[Depends(verify_csrf_token)])
def gallery_settings(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    gallery_minor_names: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    try:
        site_settings.set_gallery_minor_names(db_session, actor, value=gallery_minor_names, reason=reason)
    except ValidationFailed as exc:
        db_session.rollback()
        flash(request, " ".join(exc.errors.values()), "error")
        return RedirectResponse("/admin#gallery", status_code=303)
    db_session.commit()
    flash(request, "Gallery setting saved.", "success")
    return RedirectResponse("/admin#gallery", status_code=303)


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
