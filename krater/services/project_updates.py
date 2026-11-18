"""Progress updates and top-up requests (`docs/SPEC.md` "Progress updates and top-ups").

A project's team posts an update to say what the budget so far bought. The team, reviewers and admins read them on
the project page, and Krater posts each to the project's Slack channel (`krater.services.slack_notify`). Each one
keeps the budget as it was then (ceiling, spend, jobs and boxes started since the team's previous update), so a
reader sees what it's measured against.

A top-up request is an update with an amount and a reason, usually with the progress too: that's what it's for,
though only the reason is required. It doesn't grant anything itself: it opens and submits an amendment whose new
total is today's ceiling plus the amount, with the approved application's answers unchanged. Reviewers then decide
that amendment like any other (the approval policy, granting another amount, held grants, the ledger and Quilt), so
there is one way budget is added. Everything that stops an amendment stops a top-up (a draft in progress, a grant
still held, a project that isn't approved), and so does an amendment or top-up still in review.

Framework-free; the project pages and the API call it.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    ComputeJob,
    Project,
    ProjectRevision,
    ProjectStatus,
    ProjectUpdate,
)
from krater.services import budget, projects, teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed

#: While the project runs on its budget: approved, and while its completion is reviewed.
POSTABLE_STATUSES = (
    ProjectStatus.APPROVED,
    ProjectStatus.PENDING_COMPLETION_REVIEW,
    ProjectStatus.COMPLETION_CHANGES_REQUESTED,
)
BODY_MAX_CHARS = 10_000
REASON_MAX_CHARS = 2_000
#: A top-up larger than this is surely a typo (an extra zero); a real one that size is a full amendment.
TOPUP_MAX_CENTS = 100_000_00


@dataclass(frozen=True)
class UpdateView:
    update: ProjectUpdate
    percent_used: int | None  # of the ceiling when it was posted; None with no ceiling


def can_read(actor: Actor, project: Project) -> bool:
    """The team, reviewers and admins: the people who can see the project at all."""
    return teams.has_conflict(project, actor.user.id) or actor.is_reviewer or actor.is_admin


def can_post(actor: Actor, project: Project) -> bool:
    return teams.on_team(project, actor.user.id) and project.status in POSTABLE_STATUSES


def can_request_topup(project: Project) -> bool:
    """Whether the project could take a top-up request now (the team check is `can_post`'s): approved, with no draft
    and no amendment or top-up in review (a second would supersede it)."""
    current = project.current_revision
    has_draft = current is not None and current.submitted_at is None
    return project.status is ProjectStatus.APPROVED and not has_draft and not projects.amendment_in_review(project)


def list_for(session: Session, actor: Actor, project: Project) -> list[UpdateView]:
    """The project's updates, newest first."""
    if not can_read(actor, project):
        raise NotAllowed("Only the project's team, reviewers and admins can read its updates.")
    rows = session.scalars(
        sa.select(ProjectUpdate)
        .where(ProjectUpdate.project_id == project.id)
        .order_by(ProjectUpdate.created_at.desc(), ProjectUpdate.id)
    )
    return [UpdateView(row, _percent(row.spent_cents, row.ceiling_cents)) for row in rows]


def for_revision(session: Session, revision: ProjectRevision) -> ProjectUpdate | None:
    """The top-up request that opened `revision`, if one did."""
    return session.scalar(sa.select(ProjectUpdate).where(ProjectUpdate.revision_id == revision.id))


def post(session: Session, actor: Actor, *, project: Project, body: str) -> ProjectUpdate:
    """Post a progress update. The team only, while the project is approved or its completion is reviewed."""
    _require_post(actor, project)
    body = body.strip()
    errors = _body_errors(body, required=True)
    if errors:
        raise ValidationFailed(errors)
    update = _new_update(session, actor, project, body=body)
    session.flush()
    return update


def request_topup(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    amount_cents: int | None,
    reason: str,
    body: str = "",
    criteria_read: bool,
) -> ProjectUpdate:
    """Ask for `amount_cents` more budget: post the update and submit the amendment reviewers decide.

    `ValidationFailed` keyed by form field (`amount`, `reason`, `body`, `criteria_read`, or `application` when the
    approved application lacks an answer Krater asks for now, which only a full amendment can add). `InvalidState`
    when an amendment couldn't start now (a draft in progress, a held grant, a project that isn't approved).
    """
    _require_post(actor, project)
    body, reason = body.strip(), reason.strip()
    errors = _body_errors(body, required=False)
    if amount_cents is None or amount_cents <= 0:
        errors["amount"] = "Enter how much more you need, in dollars."
    elif amount_cents > TOPUP_MAX_CENTS:
        errors["amount"] = f"That's more than ${TOPUP_MAX_CENTS // 100:,}: for that much, start an amendment instead."
    if not reason:
        errors["reason"] = "Say what the extra budget is for."
    elif len(reason) > REASON_MAX_CHARS:
        errors["reason"] = f"Keep it to {REASON_MAX_CHARS:,} characters."
    if not criteria_read:
        errors["criteria_read"] = "Read the Ganymede Submission Criteria, then tick the box."
    if errors:
        raise ValidationFailed(errors)
    if not can_request_topup(project):
        if project.status is not ProjectStatus.APPROVED:
            raise InvalidState("A top-up can only be asked for while the project is approved.")
        raise InvalidState(
            "This project already has an amendment or top-up in progress: wait for its decision, or finish the draft "
            "on the project page, first."
        )

    ceiling = budget.ceiling_cents(session, project)
    draft = projects.start_amendment(session, actor, project=project)
    draft.budget_requested_cents = ceiling + amount_cents
    try:
        projects.submit(session, actor, project=project, criteria_read=True)
    except ValidationFailed as exc:
        # The approved application is missing an answer Krater asks for now (a question added since). The caller
        # rolls back, so no half-made amendment is left; a full amendment adds the answer.
        missing = ", ".join(sorted(exc.errors))
        raise ValidationFailed(
            {
                "application": "Your approved application is missing answers Krater asks for now "
                f"({missing}). Start a full amendment from the project page to add them; it asks for the budget too."
            }
        ) from exc
    update = _new_update(
        session, actor, project, body=body, topup_cents=amount_cents, topup_reason=reason, revision=draft
    )
    session.flush()
    return update


def _require_post(actor: Actor, project: Project) -> None:
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team can post its updates.")
    if project.status not in POSTABLE_STATUSES:
        raise InvalidState("Updates can be posted while the project is approved or its completion is reviewed.")


def _body_errors(body: str, *, required: bool) -> dict[str, str]:
    if required and not body:
        return {"body": "Say what you've done since the last update."}
    if len(body) > BODY_MAX_CHARS:
        return {"body": f"{len(body):,} characters: keep it to {BODY_MAX_CHARS:,}."}
    return {}


def _new_update(
    session: Session,
    actor: Actor,
    project: Project,
    *,
    body: str,
    topup_cents: int | None = None,
    topup_reason: str | None = None,
    revision: ProjectRevision | None = None,
) -> ProjectUpdate:
    previous = session.scalar(
        sa.select(sa.func.max(ProjectUpdate.created_at)).where(ProjectUpdate.project_id == project.id)
    )
    jobs = sa.select(sa.func.count()).select_from(ComputeJob).where(ComputeJob.project_id == project.id)
    if previous is not None:
        jobs = jobs.where(ComputeJob.created_at > previous)
    update = ProjectUpdate(
        project_id=project.id,
        author_id=actor.user.id,
        body=body,
        topup_cents=topup_cents,
        topup_reason=topup_reason,
        revision_id=revision.id if revision is not None else None,
        ceiling_cents=budget.ceiling_cents(session, project),
        spent_cents=budget.latest_spend_cents(session, project),
        jobs_since=int(session.scalar(jobs) or 0),
    )
    session.add(update)
    return update


def _percent(spent: int, ceiling: int) -> int | None:
    return None if ceiling <= 0 else round(spent * 100 / ceiling)
