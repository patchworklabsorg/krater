"""Admin views for what is happening across Krater: `/admin/activity` (every `AuditEvent`, newest first, and with
`?project=` that project's budget ledger too) and `/admin/live` (the compute queued and running on every project).
Admins only, read-only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session, selectinload

from krater.config import get_settings
from krater.db import get_session
from krater.models import AuditEvent, BudgetEntry, BudgetEntryKind, ComputeJob, Project
from krater.models.compute_job import ACTIVE_JOB_STATES
from krater.models.enums import ComputeJobState
from krater.services import budget, people
from krater.services.actor import Actor
from krater.services.errors import NotFound
from krater.weave import WeaveClient, get_weave_client
from krater.web.deps import require_admin
from krater.web.money import format_cents
from krater.web.templates import templates

router = APIRouter()

PAGE_SIZE = 50
#: How many events the admin overview shows before "All activity".
RECENT = 8

LEDGER_LABELS = {
    BudgetEntryKind.INITIAL_APPROVAL: "Approval",
    BudgetEntryKind.AMENDMENT: "Amendment",
    BudgetEntryKind.ADMIN_ADJUSTMENT: "Admin adjustment",
    BudgetEntryKind.RECLAIM: "Reclaim",
}


def action_label(action: str) -> str:
    """`admin_adjust_budget` -> "Admin adjust budget". Actions are open-ended strings, so no table of names."""
    words = action.replace(".", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _detail(key: str, value) -> str:
    if key.endswith("_cents") and isinstance(value, int):
        shown = format_cents(value)
        if key == "amount_cents" and value > 0:
            shown = "+" + shown
        return f"{key.removesuffix('_cents').replace('_', ' ')}: {shown}"
    if isinstance(value, dict):
        value = ", ".join(f"{inner}: {each}" for inner, each in value.items())
    elif isinstance(value, list):
        value = ", ".join(str(each) for each in value)
    return f"{key.replace('_', ' ')}: {value}"


def event_details(payload: dict | None) -> list[str]:
    """The payload as short "key: value" lines. Payloads hold Patchwork IDs, never names (`krater.services.audit`)."""
    return [_detail(key, value) for key, value in (payload or {}).items()]


def activity_rows(db_session: Session, weave_client: WeaveClient, events: list[AuditEvent]) -> list[dict]:
    """Each event with who did it: a name from Weave, the Patchwork ID when Weave can't say, and "Krater" for the
    worker's own events (no actor)."""
    actors = people.names(weave_client, {event.actor for event in events if event.actor is not None})
    return [
        {
            "event": event,
            "label": action_label(event.action),
            "actor": actors.get(event.actor_id, event.actor_sub or "Krater"),
            "details": event_details(event.payload),
        }
        for event in events
    ]


def recent_activity(db_session: Session, weave_client: WeaveClient) -> list[dict]:
    events = list(
        db_session.scalars(
            sa.select(AuditEvent)
            .options(selectinload(AuditEvent.actor), selectinload(AuditEvent.project))
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(RECENT)
        )
    )
    return activity_rows(db_session, weave_client, events)


@router.get("/admin/activity")
def activity_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    project: uuid.UUID | None = None,
    page: int = 1,
):
    page = max(page, 1)
    the_project = None
    query = sa.select(AuditEvent)
    if project is not None:
        the_project = db_session.get(Project, project)
        if the_project is None:
            raise NotFound(f"No project with id {project}.")
        query = query.where(AuditEvent.project_id == the_project.id)
    # One more than a page, to know whether there is a next one.
    events = list(
        db_session.scalars(
            query.options(selectinload(AuditEvent.actor), selectinload(AuditEvent.project))
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        )
    )
    has_next = len(events) > PAGE_SIZE
    events = events[:PAGE_SIZE]

    ledger = None
    if the_project is not None:
        entries = list(
            db_session.scalars(
                sa.select(BudgetEntry)
                .options(selectinload(BudgetEntry.actor))
                .where(BudgetEntry.project_id == the_project.id)
                .order_by(BudgetEntry.created_at.desc(), BudgetEntry.id.desc())
            )
        )
        names = people.names(weave_client, {entry.actor for entry in entries})
        ledger = {
            "ceiling_cents": budget.ceiling_cents(db_session, the_project),
            "rows": [
                {
                    "entry": entry,
                    "kind": LEDGER_LABELS.get(entry.kind, entry.kind.value),
                    "actor": names[entry.actor_id],
                }
                for entry in entries
            ],
        }

    return templates.TemplateResponse(
        request,
        "admin/activity.html",
        {
            "project": the_project,
            "rows": activity_rows(db_session, weave_client, events),
            "ledger": ledger,
            "page": page,
            "has_next": has_next,
        },
    )


@router.get("/admin/live")
def live_compute_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    jobs = list(
        db_session.scalars(
            sa.select(ComputeJob)
            .options(selectinload(ComputeJob.project), selectinload(ComputeJob.instances))
            .where(ComputeJob.state.in_([ComputeJobState.QUEUED, *ACTIVE_JOB_STATES]))
            .order_by(ComputeJob.queued_at)
        )
    )
    rows = []
    for job in jobs:
        live = [each for each in job.instances if each.is_live]
        rows.append({"job": job, "instance": live[-1] if live else None})
    running = [row for row in rows if row["job"].state is not ComputeJobState.QUEUED]
    queued = [row for row in rows if row["job"].state is ComputeJobState.QUEUED]
    return templates.TemplateResponse(
        request,
        "admin/live.html",
        {
            "running": running,
            "queued": queued,
            "held_cents": sum(row["job"].hold_cents for row in rows),
            "spend_cents": sum(row["job"].spend_cents for row in running),
            "compute_on": get_settings().compute_on,
            "now": datetime.now(UTC),
        },
    )
