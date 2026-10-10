"""Budget ledger arithmetic.

A project's budget ceiling is never stored directly: it's the sum of its append-only `BudgetEntry`
rows. Spend is the latest `SpendSnapshot` estimate (or 0 if none has been taken yet). Nothing here
ever updates or deletes a ledger row.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import BudgetEntry, BudgetEntryKind, Project, ProjectRevision, SpendSnapshot
from krater.services.actor import Actor


def ceiling_cents(session: Session, project: Project) -> int:
    """The project's current budget ceiling: the sum of all its `BudgetEntry.amount_cents`."""
    total = session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(BudgetEntry.amount_cents), 0)).where(
            BudgetEntry.project_id == project.id
        )
    )
    return int(total or 0)


def latest_spend_cents(session: Session, project: Project) -> int:
    """The project's most recent estimated spend, or 0 if no `SpendSnapshot` has been taken yet."""
    latest = session.scalar(
        sa.select(SpendSnapshot.estimated_spend_cents)
        .where(SpendSnapshot.project_id == project.id)
        .order_by(SpendSnapshot.taken_at.desc())
        .limit(1)
    )
    return int(latest or 0)


def remaining_cents(session: Session, project: Project) -> int:
    """Ceiling minus latest spend. Can be negative if spend has overrun the ceiling."""
    return ceiling_cents(session, project) - latest_spend_cents(session, project)


def add_entry(
    session: Session,
    *,
    project: Project,
    kind: BudgetEntryKind,
    amount_cents: int,
    actor: Actor,
    reason: str | None = None,
    revision: ProjectRevision | None = None,
) -> BudgetEntry:
    """Append a `BudgetEntry` to the project's ledger and return it. Internal: flushes, doesn't commit.

    `amount_cents` is signed (negative for reclaims and budget-decreasing amendments/adjustments).
    Used by `projects.py`'s approval/admin/reclaim effects; not meant to be called directly by routers.
    """
    entry = BudgetEntry(
        project_id=project.id,
        kind=kind,
        amount_cents=amount_cents,
        actor_id=actor.user.id,
        reason=reason,
        revision_id=revision.id if revision is not None else None,
    )
    session.add(entry)
    session.flush()
    return entry
