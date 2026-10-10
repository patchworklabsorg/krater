"""The admin overview: `/admin`, admins only -- every project grouped by status, plus a read-only
table of `ApprovalPolicy` rows."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import ApprovalPolicy, Project, ProjectStatus
from krater.services import projects as project_service
from krater.services.actor import Actor
from krater.web.deps import require_admin
from krater.web.templates import templates

router = APIRouter()


@router.get("/admin")
def admin_overview(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    del actor  # `require_admin` is the gate; nothing here is scoped to who's asking

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
        {"by_status": by_status, "policies": policies, "statuses": list(ProjectStatus)},
    )


__all__ = ["router"]
