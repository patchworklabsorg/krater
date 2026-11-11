"""Krater Compute's admin pages: `/admin/compute` (the stop switch, the organization-wide limits and the global
defaults), `/admin/compute/projects/{id}` (one project's overrides) and `/admin/compute/overview` (spend, machines and
storage by project). Admins only; every change needs a reason and writes an `AuditEvent`
(`krater.services.compute_settings`, `krater.services.compute_halt`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from krater.compute import search_quota
from krater.compute.search_pacing import LOW_QUOTA_FRACTION
from krater.config import get_settings
from krater.db import get_session
from krater.models import (
    ComputeInstance,
    ComputeJob,
    ComputeJobState,
    ComputeSettings,
    Project,
    SpendReconciliation,
)
from krater.models.compute_settings import GLOBAL_ID
from krater.services import budget, compute_halt, compute_jobs, compute_settings, compute_watch
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotFound, ValidationFailed
from krater.vast import SearchQuota
from krater.web.csrf import verify_csrf_token
from krater.web.deps import require_admin
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()


def _field_rows(
    values: dict[str, int | None], posted: dict[str, str] | None, fields=compute_settings.POLICY_FIELDS
) -> list[dict]:
    """One row per field for the form: its spec, and the value to show (what was posted, after an error)."""
    rows = []
    for field in fields:
        shown = (
            posted.get(field.name, "")
            if posted is not None
            else compute_settings.format_value(field, values[field.name])
        )
        rows.append({"field": field, "value": shown})
    return rows


def _parse_form(
    form, *, allow_blank: bool, fields=compute_settings.POLICY_FIELDS
) -> tuple[dict[str, int | None], dict[str, str], dict[str, str]]:
    """Parse every field of `fields` from `form`. Returns (values, posted strings, errors)."""
    values: dict[str, int | None] = {}
    posted: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field in fields:
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


def _switch_view(db_session: Session) -> dict:
    """The stop switch's state: whether an admin stopped compute (when, and why), or a spending cap did."""
    compute_settings.global_policy(db_session)  # the row exists
    row = db_session.get(ComputeSettings, GLOBAL_ID)
    return {
        "paused_at": row.paused_at,
        "paused_reason": row.paused_reason,
        "halted_since": row.halted_since,
        "halt_reason": row.halt_reason,
        "compute_on": get_settings().compute_on,
    }


def _global_page(
    request: Request,
    db_session: Session,
    *,
    errors=None,
    posted=None,
    reason="",
    org_errors=None,
    org_posted=None,
    org_reason="",
    switch_errors=None,
    switch_reason="",
    status_code=200,
):
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
            "org_rows": _field_rows(
                compute_settings.org_limits(db_session).values(), org_posted, compute_settings.ORG_FIELDS
            ),
            "org_errors": org_errors or {},
            "org_reason": org_reason,
            "switch": _switch_view(db_session),
            "switch_errors": switch_errors or {},
            "switch_reason": switch_reason,
            "caps": compute_halt.cap_statuses(db_session, datetime.now(UTC)),
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


@router.post("/admin/compute/limits", dependencies=[Depends(verify_csrf_token)])
async def update_org_limits(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    form = await request.form()
    reason = str(form.get("reason", ""))
    values, posted, errors = _parse_form(form, allow_blank=False, fields=compute_settings.ORG_FIELDS)
    if not errors:
        try:
            compute_settings.update_org_limits(db_session, actor, values=values, reason=reason)  # type: ignore[arg-type]
        except ValidationFailed as exc:
            errors = exc.errors
    if errors:
        db_session.rollback()
        return _global_page(
            request, db_session, org_errors=errors, org_posted=posted, org_reason=reason, status_code=422
        )
    db_session.commit()
    flash(request, "Organization limits saved.", "success")
    return RedirectResponse("/admin/compute#limits", status_code=303)


@router.post("/admin/compute/stop", dependencies=[Depends(verify_csrf_token)])
async def stop_all_compute(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    form = await request.form()
    reason = str(form.get("reason", ""))
    try:
        compute_halt.pause(db_session, actor, reason=reason, now=datetime.now(UTC))
    except ValidationFailed as exc:
        db_session.rollback()
        return _global_page(request, db_session, switch_errors=exc.errors, switch_reason=reason, status_code=422)
    except InvalidState as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/admin/compute", status_code=303)
    db_session.commit()
    flash(
        request,
        "Compute stopped. Within a minute Krater stops every running machine and starts nothing new.",
        "success",
    )
    return RedirectResponse("/admin/compute", status_code=303)


@router.post("/admin/compute/start", dependencies=[Depends(verify_csrf_token)])
async def start_all_compute(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    form = await request.form()
    reason = str(form.get("reason", ""))
    try:
        compute_halt.resume(db_session, actor, reason=reason, now=datetime.now(UTC))
    except ValidationFailed as exc:
        db_session.rollback()
        return _global_page(request, db_session, switch_errors=exc.errors, switch_reason=reason, status_code=422)
    except InvalidState as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/admin/compute", status_code=303)
    db_session.commit()
    flash(request, "Compute is back on. Queued jobs start on the next pass, within a minute.", "success")
    return RedirectResponse("/admin/compute", status_code=303)


# -- Overview ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    days: int | None  # None: the calendar month (`month`) or all time (`all`)


PERIODS = (
    Period("7d", "Last 7 days", 7),
    Period("30d", "Last 30 days", 30),
    Period("month", "This month", None),
    Period("all", "All time", None),
)
_PERIODS = {period.key: period for period in PERIODS}


def _period_start(period: Period, now: datetime) -> datetime | None:
    if period.days is not None:
        return now - timedelta(days=period.days)
    if period.key == "month":
        return compute_halt.month_start(now)
    return None


@router.get("/admin/compute/overview")
def compute_overview_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    period: Annotated[str, Query()] = "30d",
):
    now = datetime.now(UTC)
    chosen = _PERIODS.get(period, _PERIODS["30d"])
    in_period = compute_halt.spend_since(db_session, _period_start(chosen, now))
    all_time = compute_halt.spend_since(db_session, None)
    storage = compute_watch.storage_by_project(db_session)
    machines = dict(
        db_session.execute(
            sa.select(ComputeJob.project_id, sa.func.count())
            .select_from(ComputeInstance)
            .join(ComputeJob, ComputeJob.id == ComputeInstance.job_id)
            .where(ComputeInstance.state.in_(compute_jobs.LIMITED_STATES))
            .group_by(ComputeJob.project_id)
        ).all()
    )
    queued = dict(
        db_session.execute(
            sa.select(ComputeJob.project_id, sa.func.count())
            .where(ComputeJob.state == ComputeJobState.QUEUED)
            .group_by(ComputeJob.project_id)
        ).all()
    )
    latest_checks = {
        check.project_id: check
        for check in db_session.scalars(
            sa.select(SpendReconciliation)
            .ext(distinct_on(SpendReconciliation.project_id))
            .order_by(SpendReconciliation.project_id, SpendReconciliation.checked_at.desc())
        )
    }
    project_ids = set(all_time) | set(storage) | set(machines) | set(queued)
    projects = db_session.scalars(sa.select(Project).where(Project.id.in_(project_ids))).all() if project_ids else []
    rows = []
    for project in projects:
        policy = compute_settings.effective_policy(db_session, project)
        ceiling = budget.ceiling_cents(db_session, project)
        total = all_time.get(project.id, 0)
        rows.append(
            {
                "project": project,
                "period_cents": in_period.get(project.id, 0),
                "total_cents": total,
                "ceiling_cents": ceiling,
                "remaining_cents": ceiling - budget.latest_spend_cents(db_session, project),
                "machines": machines.get(project.id, 0),
                "max_machines": policy.max_machines,
                "queued": queued.get(project.id, 0),
                "storage_bytes": storage.get(project.id, 0),
                "quota_bytes": policy.storage_quota_gb * 1024**3,
                "check": latest_checks.get(project.id),
            }
        )
    rows.sort(key=lambda row: (row["period_cents"], row["total_cents"], row["storage_bytes"]), reverse=True)

    settings = get_settings()
    stored, images, counted_at = compute_watch.storage_totals(db_session)
    checks = list(latest_checks.values())
    return templates.TemplateResponse(
        request,
        "admin/compute_overview.html",
        {
            "periods": PERIODS,
            "period": chosen,
            "rows": rows,
            "period_total_cents": sum(row["period_cents"] for row in rows),
            "caps": compute_halt.cap_statuses(db_session, now),
            "switch": _switch_view(db_session),
            "credit": compute_watch.latest_credit(db_session),
            "credit_alert_cents": compute_settings.org_limits(db_session).credit_alert_cents,
            "stored_bytes": stored,
            "image_bytes": images,
            "counted_at": counted_at,
            "capacity_bytes": settings.compute_store_capacity_gb * 1024**3,
            "store_alert_percent": compute_watch.STORE_ALERT_PERCENT,
            "default_quota_gb": compute_settings.global_policy(db_session).storage_quota_gb,
            "checks_estimate_microusd": sum(check.estimate_microusd for check in checks),
            "checks_actual_microusd": sum(check.actual_microusd for check in checks),
            "machines_total": sum(machines.values()),
            "queued_total": sum(queued.values()),
            "now": now,
        },
    )


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
