"""Budget grants that wait for the team's verification in Weave (docs/SPEC.md "Verification before funding").

A project is approved whatever its team's verification. The budget that approval grants (a proposal approval, an
amendment that raises the ceiling, an admin increase) goes into the ledger, and so to Quilt as `budget.committed`,
only once Weave has cleared every team member (`krater.services.clearance`). Until then it is a `HeldGrant`.

- **Grant.** `grant` asks Weave fresh. Cleared: it writes the `BudgetEntry` at once. Not cleared, or Weave can't be
  reached: it writes a `HeldGrant` instead, and the approval still succeeds.
- **One at a time.** A project has at most one pending hold. While it's pending, nothing else may change the
  project's budget: amendments, admin adjustments and reclaims are refused (`require_none_pending`). The ceiling
  can't move, so the amount decided at approval is still right at release.
- **Release.** `release_ready`, a periodic worker task, asks Weave about each held project's team as it stands then
  and writes the entry, with the approver as its actor, once every member is cleared. Weave down keeps it held.
- **Cancel.** Withdrawing or completing the project cancels its hold (`cancel_pending`).
- **No override.** An admin can't release a hold; the team is cleared in Weave.
- **Funding holds.** While `Project.funding_hold` is set (`krater.services.funding_holds`), every grant is held
  whatever the team's verification, and nothing is released. Clearing the flag releases the hold at once if the team
  is cleared (`release_project`); otherwise it waits for verification as usual.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import BudgetEntry, BudgetEntryKind, HeldGrant, Project, ProjectRevision, ProjectStatus
from krater.services import audit, budget, clearance, quilt_events
from krater.services.actor import Actor
from krater.services.errors import InvalidState
from krater.weave import WeaveClient, WeaveUnavailableError

logger = logging.getLogger(__name__)

AUDIT_GRANT_HELD = "budget_grant_held"
AUDIT_GRANT_RELEASED = "budget_grant_released"
AUDIT_GRANT_CANCELLED = "budget_grant_cancelled"
HELD_MESSAGE = (
    "This project's budget is held until every team member is verified in Weave, so its budget can't change until "
    "then. Withdrawing the project cancels the hold."
)
FUNDING_HELD_MESSAGE = (
    "This project's budget is held until an admin clears its funding hold, so its budget can't change until then. "
    "Withdrawing the project cancels the hold."
)
#: Projects whose holds stay pending: a finished project's hold is cancelled, never released.
_OPEN_STATUSES = (
    ProjectStatus.APPROVED,
    ProjectStatus.PENDING_COMPLETION_REVIEW,
    ProjectStatus.COMPLETION_CHANGES_REQUESTED,
)


@dataclass(frozen=True)
class Granted:
    """What `grant` did: wrote `entry`, or held the grant as `held`."""

    entry: BudgetEntry | None = None
    held: HeldGrant | None = None


def pending(session: Session, project: Project) -> HeldGrant | None:
    """The project's pending hold, if any."""
    return session.scalar(
        sa.select(HeldGrant).where(
            HeldGrant.project_id == project.id, HeldGrant.released_at.is_(None), HeldGrant.cancelled_at.is_(None)
        )
    )


#: How a held grant's kind reads on the admin overview.
KIND_LABELS = {
    BudgetEntryKind.INITIAL_APPROVAL: "Approval",
    BudgetEntryKind.AMENDMENT: "Amendment",
    BudgetEntryKind.ADMIN_ADJUSTMENT: "Admin increase",
}


def all_pending(session: Session) -> list[HeldGrant]:
    """Every pending hold, oldest first: what the admin overview lists."""
    return list(
        session.scalars(
            sa.select(HeldGrant)
            .where(HeldGrant.released_at.is_(None), HeldGrant.cancelled_at.is_(None))
            .order_by(HeldGrant.created_at)
        )
    )


def held_project_ids(session: Session, project_ids: list) -> set:
    """Which of `project_ids` have a pending hold: one query, for the lists that badge them."""
    if not project_ids:
        return set()
    return set(
        session.scalars(
            sa.select(HeldGrant.project_id).where(
                HeldGrant.project_id.in_(project_ids),
                HeldGrant.released_at.is_(None),
                HeldGrant.cancelled_at.is_(None),
            )
        )
    )


def require_none_pending(session: Session, project: Project) -> None:
    """Refuse a budget change while a grant is held: the hold's amount was decided against today's ceiling."""
    if pending(session, project) is not None:
        raise InvalidState(FUNDING_HELD_MESSAGE if project.funding_hold is not None else HELD_MESSAGE)


def _not_cleared(session: Session, weave_client: WeaveClient, project: Project) -> dict | None:
    """What the grant is waiting on right now, or `None` when nothing holds it: a funding hold (Weave isn't asked
    then), or the team, which Weave hasn't cleared. Weave down counts as not cleared.

    Only Patchwork IDs, for the audit payload: Krater stores no names, and not why a member isn't cleared (that
    can say they're under 18). The project page asks Weave for the reasons when it's shown.
    """
    if project.funding_hold is not None:
        return {"funding_hold": project.funding_hold.value}
    try:
        members = clearance.members_not_cleared(session, weave_client, project, fresh=True)
    except WeaveUnavailableError:
        return {"weave_unavailable": True}
    if not members:
        return None
    return {"waiting_on": [member.user.weave_sub for member in members]}


def grant(
    session: Session,
    weave_client: WeaveClient,
    *,
    project: Project,
    kind: BudgetEntryKind,
    amount_cents: int,
    actor: Actor,
    reason: str | None = None,
    revision: ProjectRevision | None = None,
) -> Granted:
    """Grant `amount_cents` (positive) to `project`: a `BudgetEntry` now if Weave clears the team, else a
    `HeldGrant`. The caller holds the project's row lock and calls `quilt_events.sync_project` after."""
    require_none_pending(session, project)
    waiting = _not_cleared(session, weave_client, project)
    if waiting is None:
        entry = budget.add_entry(
            session,
            project=project,
            kind=kind,
            amount_cents=amount_cents,
            actor=actor,
            reason=reason,
            revision=revision,
        )
        return Granted(entry=entry)
    held = HeldGrant(
        project_id=project.id,
        kind=kind,
        amount_cents=amount_cents,
        actor_id=actor.user.id,
        reason=reason,
        revision_id=revision.id if revision is not None else None,
    )
    session.add(held)
    session.flush()
    audit.record(
        session,
        actor,
        AUDIT_GRANT_HELD,
        project=project,
        payload={"amount_cents": amount_cents, "kind": kind.value, **waiting},
    )
    return Granted(held=held)


def cancel_pending(session: Session, project: Project, *, actor: Actor | None, reason: str) -> HeldGrant | None:
    """Cancel the project's pending hold (it was withdrawn or completed before the team was cleared)."""
    held = pending(session, project)
    if held is None:
        return None
    held.cancelled_at = datetime.now(UTC)
    held.cancel_reason = reason
    audit.record(
        session,
        actor,
        AUDIT_GRANT_CANCELLED,
        project=project,
        payload={"amount_cents": held.amount_cents},
        reason=reason,
    )
    session.flush()
    return held


def release_ready(session: Session, weave_client: WeaveClient, *, now: datetime) -> int:
    """Write the ledger entry of every held grant whose team Weave now clears. Commits after each project, so one
    project's failure keeps the others' releases. Returns how many it released."""
    ids = list(
        session.scalars(
            sa.select(HeldGrant.id)
            .where(HeldGrant.released_at.is_(None), HeldGrant.cancelled_at.is_(None))
            .order_by(HeldGrant.created_at)
        )
    )
    released = 0
    for held_id in ids:
        try:
            if _release_one(session, weave_client, held_id, now=now):
                released += 1
            session.commit()
        except Exception:
            logger.exception("krater.grants: releasing held grant %s failed", held_id)
            session.rollback()
    return released


def _release_one(session: Session, weave_client: WeaveClient, held_id, *, now: datetime) -> bool:
    held = session.get(HeldGrant, held_id)
    project = held.project
    # The project row lock every budget change takes first, then re-read the hold under it: an approval, a withdrawal
    # or another release may have got there first.
    session.execute(sa.select(Project.id).where(Project.id == project.id).with_for_update())
    session.refresh(held)
    session.refresh(project)
    return _release_locked(session, weave_client, held, project, now=now)


def release_project(session: Session, weave_client: WeaveClient, project: Project, *, now: datetime) -> bool:
    """Release `project`'s pending hold now if nothing holds it any more. The caller holds the project's row lock
    (an admin clearing its funding hold); it doesn't commit."""
    held = pending(session, project)
    if held is None:
        return False
    return _release_locked(session, weave_client, held, project, now=now)


def _release_locked(
    session: Session, weave_client: WeaveClient, held: HeldGrant, project: Project, *, now: datetime
) -> bool:
    if not held.pending or project.status not in _OPEN_STATUSES:
        return False
    if _not_cleared(session, weave_client, project) is not None:
        return False
    approver = Actor(user=held.actor, groups=frozenset())
    entry = budget.add_entry(
        session,
        project=project,
        kind=held.kind,
        amount_cents=held.amount_cents,
        actor=approver,
        reason=held.reason,
        revision=held.revision,
    )
    held.released_at = now
    held.budget_entry_id = entry.id
    audit.record(session, None, AUDIT_GRANT_RELEASED, project=project, payload={"amount_cents": held.amount_cents})
    session.flush()
    quilt_events.sync_project(session, project)
    return True


__all__ = [
    "AUDIT_GRANT_CANCELLED",
    "AUDIT_GRANT_HELD",
    "AUDIT_GRANT_RELEASED",
    "FUNDING_HELD_MESSAGE",
    "HELD_MESSAGE",
    "KIND_LABELS",
    "Granted",
    "all_pending",
    "cancel_pending",
    "grant",
    "held_project_ids",
    "pending",
    "release_project",
    "release_ready",
    "require_none_pending",
]
