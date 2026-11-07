"""Funding holds: approving a project whose budget waits on Patchwork, not on its team (docs/SPEC.md "Funding holds").

A reviewer approving on the web, or an admin deciding, can mark the approval *awaiting funding* or *awaiting 3rd party
review*. The approval counts and the project is `approved`, but while `Project.funding_hold` is set:

- every grant is held (`krater.services.grants` treats the flag like an uncleared team), so nothing reaches the
  ledger or Quilt;
- no compute starts (`require_not_held`, next to `clearance.require_team_cleared`). Machines already running are
  left alone: an admin who flags a funded project stops them separately if they must.

Only an admin sets or clears the flag after approval, with a reason (an `AuditEvent` and a post in the project's
channel). Clearing it releases the held grant at once if Weave clears the team, and otherwise leaves it on the usual
verification hold. An admin can't clear the flag on a project they lead or are on: that would fund their own project.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from krater.models import FundingHold, Project, ProjectStatus
from krater.services import audit, grants, teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.services.projects import _OWN_PROJECT_MESSAGE, _lock_project
from krater.weave import WeaveClient

LABELS = {
    FundingHold.AWAITING_FUNDING: "Awaiting funding",
    FundingHold.AWAITING_THIRD_PARTY_REVIEW: "Awaiting 3rd party review",
}
#: What the team reads on its project page while the flag is set.
EXPLANATIONS = {
    FundingHold.AWAITING_FUNDING: "Approved, and waiting on Patchwork to fund it.",
    FundingHold.AWAITING_THIRD_PARTY_REVIEW: "Approved, and waiting on a review by a third party.",
}
#: An approved project whose team Weave hasn't cleared: shown, never set. Weave decides it, not an admin.
AWAITING_VERIFICATION = "Awaiting verification"
AUDIT_SET = "admin_set_funding_hold"
AUDIT_CLEAR = "admin_clear_funding_hold"
#: Statuses a hold can be set on: approved, and still open.
HOLDABLE_STATUSES = (
    ProjectStatus.APPROVED,
    ProjectStatus.PENDING_COMPLETION_REVIEW,
    ProjectStatus.COMPLETION_CHANGES_REQUESTED,
)


class FundingHeld(InvalidState):
    """The project's compute can't start while a funding hold is set."""

    def __init__(self, hold: FundingHold) -> None:
        super().__init__(
            f"This project is approved but {LABELS[hold].lower()}, so its compute can't start yet. "
            "An admin clears the hold once it's settled."
        )


def parse(value: str | None) -> FundingHold | None:
    """A form's value: blank means no hold. Raises `ValidationFailed` for anything else unknown."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return FundingHold(value)
    except ValueError:
        raise ValidationFailed({"funding_hold": "Choose one of the holds."}) from None


def approval_label(project: Project, *, verification_held: bool) -> str | None:
    """What an approved project waits on, for its status badge: its funding hold, else "Awaiting verification" when
    `verification_held` (its budget is held for the team's verification, or Weave hasn't cleared the team), else
    `None`. `None` too for a project that isn't approved and open."""
    if project.status not in HOLDABLE_STATUSES:
        return None
    if project.funding_hold is not None:
        return LABELS[project.funding_hold]
    return AWAITING_VERIFICATION if verification_held else None


def require_not_held(project: Project) -> None:
    """The funding gate before `project`'s compute is used."""
    if project.funding_hold is not None:
        raise FundingHeld(project.funding_hold)


def set_hold(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    hold: FundingHold | None,
    reason: str,
    weave_client: WeaveClient,
    now: datetime,
) -> bool:
    """Admin: set, change or clear (`hold=None`) `project`'s funding hold. Returns whether clearing it released a
    held grant now. The caller commits, then posts the change in the project's channel."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may set or clear a funding hold.")
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})
    if hold is None and teams.has_conflict(project, actor.user.id):
        raise NotAllowed(_OWN_PROJECT_MESSAGE)

    _lock_project(session, project)
    if project.status not in HOLDABLE_STATUSES:
        raise InvalidState("A funding hold can only be set on an approved project.")
    if hold == project.funding_hold:
        raise InvalidState("No hold is set." if hold is None else f"The project is already {LABELS[hold].lower()}.")

    previous = project.funding_hold
    project.funding_hold = hold
    audit.record(
        session,
        actor,
        AUDIT_CLEAR if hold is None else AUDIT_SET,
        project=project,
        payload={"funding_hold": hold.value if hold else None, "previous": previous.value if previous else None},
        reason=reason,
    )
    session.flush()
    if hold is not None:
        return False
    return grants.release_project(session, weave_client, project, now=now)


def describe_change(previous: FundingHold | None, hold: FundingHold | None) -> str:
    """The line the channel post carries."""
    if hold is None:
        return f"Hold cleared: no longer {LABELS[previous].lower()}." if previous else "Hold cleared."
    return f"Marked {LABELS[hold].lower()}."


__all__ = [
    "AWAITING_VERIFICATION",
    "HOLDABLE_STATUSES",
    "approval_label",
    "AUDIT_CLEAR",
    "AUDIT_SET",
    "EXPLANATIONS",
    "LABELS",
    "FundingHeld",
    "describe_change",
    "parse",
    "require_not_held",
    "set_hold",
]
