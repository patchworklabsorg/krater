"""Krater Compute's admin settings: `/admin/compute` (the global defaults) and `/admin/compute/projects/{id}` (one
project's overrides). Admins only; every change needs a reason and writes an `AuditEvent`
(`krater.services.compute_settings`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.compute import search_quota
from krater.compute.search_pacing import LOW_QUOTA_FRACTION
from krater.db import get_session
from krater.models import Project, SpendReconciliation
from krater.services import compute_settings
from krater.services.actor import Actor
from krater.services.errors import NotFound, ValidationFailed
from krater.vast import SearchQuota
from krater.web.csrf import verify_csrf_token
from krater.web.deps import require_admin
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()


def _field_rows(values: dict[str, int | None], posted: dict[str, str] | None) -> list[dict]:
    """One row per policy field for the form: its spec, and the value to show (what was posted, after an error)."""
    rows = []
    for field in compute_settings.POLICY_FIELDS:
        shown = (
            posted.get(field.name, "")
            if posted is not None
            else compute_settings.format_value(field, values[field.name])
        )
        rows.append({"field": field, "value": shown})
    return rows


def _parse_form(form, *, allow_blank: bool) -> tuple[dict[str, int | None], dict[str, str], dict[str, str]]:
    """Parse every policy field from `form`. Returns (values, posted strings, errors)."""
    values: dict[str, int | None] = {}
    posted: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field in compute_settings.POLICY_FIELDS:
        raw = str(form.get(field.name, ""))
        posted[field.name] = raw
        try:
            value = compute_settings.parse_value(field, raw)
        except ValidationFailed as exc:
            errors.update(exc.errors)
            continue
        if value is None and not allow_blank:
            errors[field.name] = f"Enter a value for {field.label.lower()}."
        values[field.name] = value
    return values, posted, errors


def _quota_view(db_session: Session, now: datetime) -> dict | None:
    row = search_quota.latest(db_session)
    if row is None:
        return None
    quota = SearchQuota(limit=row.limit_rows, remaining=row.remaining_rows, reset_at=row.reset_at)
    return {
        "row": row,
        "reset_passed": now >= row.reset_at,
        "exhausted": quota.exhausted(now),
        "low": quota.fraction_left(now) < LOW_QUOTA_FRACTION,
    }


def _global_page(request: Request, db_session: Session, *, errors=None, posted=None, reason="", status_code=200):
    policy = compute_settings.global_policy(db_session)
    overrides = [
        {
            "project": project,
            "fields": [
                (field, compute_settings.format_value(field, values[field.name]))
                for field in compute_settings.POLICY_FIELDS
                if values[field.name] is not None
            ],
        }
        for project, values in compute_settings.projects_with_overrides(db_session)
    ]
    return templates.TemplateResponse(
        request,
        "admin/compute.html",
        {
            "rows": _field_rows(policy.values(), posted),
            "quota": _quota_view(db_session, datetime.now(UTC)),
            "overrides": overrides,
            "mismatches": [
                (check, db_session.get(Project, check.project_id))
                for check in db_session.scalars(
                    sa.select(SpendReconciliation)
                    .where(SpendReconciliation.alerted.is_(True))
                    .order_by(SpendReconciliation.checked_at.desc())
                    .limit(20)
                )
            ],
            "errors": errors or {},
            "reason": reason,
        },
        status_code=status_code,
    )


@router.get("/admin/compute")
def compute_settings_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    return _global_page(request, db_session)


@router.post("/admin/compute", dependencies=[Depends(verify_csrf_token)])
async def update_compute_settings(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    form = await request.form()
    reason = str(form.get("reason", ""))
    values, posted, errors = _parse_form(form, allow_blank=False)
    if not errors:
        try:
            compute_settings.update_global(db_session, actor, values=values, reason=reason)  # type: ignore[arg-type]
        except ValidationFailed as exc:
            errors = exc.errors
    if errors:
        db_session.rollback()
        return _global_page(request, db_session, errors=errors, posted=posted, reason=reason, status_code=422)
    db_session.commit()
    flash(request, "Compute defaults saved.", "success")
    return RedirectResponse("/admin/compute", status_code=303)


def _get_project(db_session: Session, project_id: uuid.UUID) -> Project:
    project = db_session.get(Project, project_id)
    if project is None:
        raise NotFound("No such project.")
    return project


def _project_page(
    request: Request, db_session: Session, project: Project, *, errors=None, posted=None, reason="", status_code=200
):
    overrides = compute_settings.project_overrides(db_session, project)
    defaults = compute_settings.global_policy(db_session).values()
    rows = _field_rows(overrides, posted)
    for row in rows:
        row["default"] = compute_settings.format_value(row["field"], defaults[row["field"].name])
    return templates.TemplateResponse(
        request,
        "admin/compute_project.html",
        {"project": project, "rows": rows, "errors": errors or {}, "reason": reason},
        status_code=status_code,
    )


@router.get("/admin/compute/projects/{project_id}")
def project_compute_settings_page(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    return _project_page(request, db_session, _get_project(db_session, project_id))


@router.post("/admin/compute/projects/{project_id}", dependencies=[Depends(verify_csrf_token)])
async def update_project_compute_settings(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    project = _get_project(db_session, project_id)
    form = await request.form()
    reason = str(form.get("reason", ""))
    values, posted, errors = _parse_form(form, allow_blank=True)
    if not errors:
        try:
            compute_settings.set_project_overrides(db_session, actor, project=project, overrides=values, reason=reason)
        except ValidationFailed as exc:
            errors = exc.errors
    if errors:
        db_session.rollback()
        return _project_page(request, db_session, project, errors=errors, posted=posted, reason=reason, status_code=422)
    db_session.commit()
    flash(request, "Compute settings for this project saved.", "success")
    return RedirectResponse(f"/admin/compute/projects/{project.id}", status_code=303)


__all__ = ["router"]
