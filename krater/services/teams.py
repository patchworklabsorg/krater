"""Project teams: a lead (the project's submitter) plus the members they invite.

Any Ganymede member can be invited, by Patchwork ID or email (found in Weave; Krater stores only the Patchwork
ID). An invitation grants nothing until the invitee accepts it while signed in. From then on they are on the team:
they can see the project, edit and submit its drafts, and use its compute, the same as the lead. Teams have no size
limit, and people can be added at any time until the project is completed or withdrawn.

Two different questions, kept apart on purpose:

- `on_team`: may this person act for the project? The lead and accepted members only.
- `has_conflict`: is this person too close to the project to review it? The lead and *everyone invited*, accepted
  or not, checked against the team as it is at the moment of the decision, never a snapshot. Otherwise a reviewer
  could approve a project and then join it, or join one between submission and review.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeJob, Project, ProjectMember, ProjectStatus
from krater.models.compute_job import FINISHED_JOB_STATES
from krater.services import audit, budget, guardian, people
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.weave import WeaveClient

#: A project's team stops changing once the project is over.
CLOSED_STATUSES = frozenset({ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN})


def on_team(project: Project, user_id: uuid.UUID) -> bool:
    """Whether `user_id` leads `project` or has accepted an invitation to its team."""
    return user_id == project.submitter_id or any(m.user_id == user_id and m.accepted for m in project.members)


def team_user_ids(project: Project) -> list[uuid.UUID]:
    """The lead first, then accepted members in the order they were invited."""
    return [project.submitter_id, *(m.user_id for m in project.members if m.accepted)]


def conflict_user_ids(project: Project) -> set[uuid.UUID]:
    """The lead and everyone invited, accepted or not: none of them may review the project."""
    return {project.submitter_id, *(m.user_id for m in project.members)}


def has_conflict(project: Project, user_id: uuid.UUID) -> bool:
    return user_id in conflict_user_ids(project)


def conflict_clause(user_id: uuid.UUID) -> sa.ColumnElement[bool]:
    """SQL for "`user_id` leads `Project` or is invited to it", for queries over many projects."""
    invited = sa.exists().where(ProjectMember.project_id == Project.id, ProjectMember.user_id == user_id)
    return sa.or_(Project.submitter_id == user_id, invited)


def _require_open(project: Project) -> None:
    if project.status in CLOSED_STATUSES:
        raise InvalidState(f"The project is {project.status.value}; its team can't change any more.")


def invite(session: Session, actor: Actor, weave_client: WeaveClient, *, project: Project, query: str) -> ProjectMember:
    """Invite the Ganymede member whose Patchwork ID or email is `query` to `project`'s team.

    The team (or an admin) invites. The invitee must be an active member in Weave. Raises `ValidationFailed` on the
    `member` field for an unknown person or someone already on (or invited to) the team; `WeaveUnavailableError`
    propagates. Writes an audit event. Flushes, doesn't commit.
    """
    if not (on_team(project, actor.user.id) or actor.is_admin):
        raise NotAllowed("Only the project's team or an admin can invite people to it.")
    _require_open(project)
    if not query.strip():
        raise ValidationFailed({"member": "Enter a Patchwork ID or email."})

    record = people.find_member(weave_client, query)
    if record is None:
        raise ValidationFailed({"member": "No Ganymede member has that Patchwork ID or email."})
    user = people.user_for(session, record)
    if user.id == project.submitter_id:
        raise ValidationFailed({"member": "They lead this project already."})
    existing = next((m for m in project.members if m.user_id == user.id), None)
    if existing is not None:
        message = "They're on the team already." if existing.accepted else "They're invited already."
        raise ValidationFailed({"member": message})

    member = ProjectMember(project_id=project.id, user_id=user.id, invited_by_id=actor.user.id)
    project.members.append(member)
    session.flush()
    audit.record(session, actor, "team_invite", project=project, payload={"member_sub": user.weave_sub})
    return member


def _own_invitation(session: Session, actor: Actor, member_id: uuid.UUID) -> ProjectMember:
    member = session.get(ProjectMember, member_id)
    if member is None or member.user_id != actor.user.id:
        raise NotFound("No such invitation for you.")
    return member


def accept(
    session: Session,
    actor: Actor,
    weave_client: WeaveClient,
    *,
    member_id: uuid.UUID,
    now: datetime | None = None,
) -> ProjectMember:
    """Accept the actor's own invitation. Only a Ganymede member can join a team, and joining a project that already
    has budget passes the guardian gate (`guardian.require_member_cleared`), as the grant itself did."""
    member = _own_invitation(session, actor, member_id)
    if not actor.is_member:
        raise NotAllowed("Only Ganymede members can join a project team.")
    # The project row lock every grant takes first (`projects._lock_project`): without it, a join racing an approval
    # could read the budget as 0 and skip the gate while the approval checked the team from before the join.
    session.refresh(member.project, with_for_update=True)
    _require_open(member.project)
    if member.accepted:
        raise InvalidState("You're on this team already.")
    if budget.ceiling_cents(session, member.project) > 0:
        guardian.require_member_cleared(weave_client, actor.user, member.project)
    member.accepted_at = now or datetime.now(UTC)
    session.flush()
    audit.record(session, actor, "team_join", project=member.project, payload={"member_sub": actor.user.weave_sub})
    return member


def decline(session: Session, actor: Actor, *, member_id: uuid.UUID) -> Project:
    """Turn down the actor's own pending invitation."""
    member = _own_invitation(session, actor, member_id)
    if member.accepted:
        raise InvalidState("You've joined this team; leave it from the project page instead.")
    project = member.project
    project.members.remove(member)
    session.flush()
    audit.record(session, actor, "team_decline", project=project, payload={"member_sub": actor.user.weave_sub})
    return project


def remove(session: Session, actor: Actor, *, project: Project, member_id: uuid.UUID) -> uuid.UUID | None:
    """Remove a member or cancel an invitation. The lead or an admin can remove anyone; a member can leave.

    The lead can't be removed: they answer for the project. Nor can anyone with a job or box on the project that
    hasn't finished: it would keep running on the project's budget and secrets after they left. Writes an audit
    event. Returns the user who left the team, for taking them out of its Slack channel; `None` for a cancelled
    invitation, whose person was never in it.
    """
    member = next((m for m in project.members if m.id == member_id), None)
    if member is None:
        raise NotFound("No such team member.")
    leaving = member.user_id == actor.user.id
    if not (leaving or actor.user.id == project.submitter_id or actor.is_admin):
        raise NotAllowed("Only the project's lead or an admin can remove someone from its team.")
    _require_open(project)
    unfinished = session.scalar(
        sa.select(sa.func.count())
        .select_from(ComputeJob)
        .where(
            ComputeJob.project_id == project.id,
            ComputeJob.submitter_id == member.user_id,
            ComputeJob.state.not_in(FINISHED_JOB_STATES),
        )
    )
    if unfinished:
        who = "You have" if leaving else "They have"
        raise InvalidState(
            f"{who} {unfinished} job{'s' if unfinished != 1 else ''} or box{'es' if unfinished != 1 else ''} on this "
            "project that hasn't finished. Cancel them first: they use the project's budget and secrets."
        )
    sub = member.user.weave_sub
    left = member.user_id if member.accepted else None
    project.members.remove(member)
    session.flush()
    audit.record(
        session, actor, "team_leave" if leaving else "team_remove", project=project, payload={"member_sub": sub}
    )
    return left


def pending_invitations(session: Session, user_id: uuid.UUID) -> list[ProjectMember]:
    """`user_id`'s invitations they haven't answered, on projects still open, newest first."""
    stmt = (
        sa.select(ProjectMember)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            ProjectMember.user_id == user_id,
            ProjectMember.accepted_at.is_(None),
            Project.status.not_in(CLOSED_STATUSES),
        )
        .order_by(ProjectMember.created_at.desc())
    )
    return list(session.scalars(stmt))


__all__ = [
    "CLOSED_STATUSES",
    "accept",
    "conflict_clause",
    "conflict_user_ids",
    "decline",
    "has_conflict",
    "invite",
    "on_team",
    "pending_invitations",
    "remove",
    "team_user_ids",
]
