"""Project teams (`krater.services.teams`): a lead plus members they invite by Patchwork ID or email. Invitations
grant nothing until accepted, but an invitee already can't review the project."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    AuditEvent,
    BudgetEntryKind,
    ComputeEscalation,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    ProjectStatus,
    ReviewDecision,
    ReviewSource,
    RevisionOutcome,
)
from krater.services import approval_policy, projects, teams
from krater.services import budget as budget_service
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed, VerificationRequired
from krater.weave import StubWeaveClient
from tests.factories import new_project
from tests.services.conftest import PROFILES, add_to_team


def _project(db_session: Session, lead: Actor):
    return new_project(db_session, lead, title="Rover", write_up="w", budget_requested_cents=1000)


def _email(actor: Actor) -> str:
    return PROFILES[actor.user.weave_sub][1]


def test_the_lead_invites_by_patchwork_id_and_access_starts_at_accept(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    by_id = make_actor(groups=frozenset({GROUP_MEMBER}))
    # No Weave role at all: every Patchwork member Weave lets use Krater can be invited.
    no_role = make_actor(groups=frozenset())
    project = _project(db_session, member)

    first = teams.invite(db_session, member, weave, project=project, query=by_id.user.weave_sub.lower())
    second = teams.invite(db_session, member, weave, project=project, query=no_role.user.weave_sub)

    assert [m.user_id for m in project.members] == [by_id.user.id, no_role.user.id]
    assert not teams.on_team(project, by_id.user.id)
    assert teams.has_conflict(project, by_id.user.id)
    with pytest.raises(NotAllowed):
        projects.update_draft(db_session, by_id, project=project, write_up="sneaky")

    teams.accept(db_session, by_id, weave, member_id=first.id)

    assert teams.on_team(project, by_id.user.id)
    assert teams.team_user_ids(project) == [member.user.id, by_id.user.id]
    projects.update_draft(db_session, by_id, project=project, write_up="Ours now.")
    assert project.current_revision.write_up == "Ours now."
    assert second.accepted_at is None
    actions = [e.action for e in db_session.query(AuditEvent).filter_by(project_id=project.id)]
    assert actions.count("team_invite") == 2 and "team_join" in actions


def test_an_accepted_teammate_can_invite_more_people_later(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    teammate = make_actor(groups=frozenset({GROUP_MEMBER}))
    newcomer = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)
    add_to_team(db_session, project, teammate)
    project.status = ProjectStatus.APPROVED

    teams.invite(db_session, teammate, weave, project=project, query=newcomer.user.weave_sub)

    assert newcomer.user.id in teams.conflict_user_ids(project)


@pytest.mark.parametrize(
    "query,message",
    [
        ("", "Enter a Patchwork ID"),
        ("nobody@example.com", "can't find people by email"),
        ("PWL0NOSUCHONE", "No Patchwork"),
    ],
)
def test_unknown_people_are_refused(
    db_session: Session, member: Actor, weave: StubWeaveClient, query: str, message: str
) -> None:
    project = _project(db_session, member)

    with pytest.raises(ValidationFailed) as exc:
        teams.invite(db_session, member, weave, project=project, query=query)

    assert message in exc.value.errors["member"]


def test_an_email_is_refused_even_for_a_member(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    teammate = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)

    with pytest.raises(ValidationFailed) as exc:
        teams.invite(db_session, member, weave, project=project, query=_email(teammate))

    assert "Patchwork ID" in exc.value.errors["member"]


@pytest.mark.parametrize("gone", ["removed", "inactive"])
def test_only_people_weave_lets_use_krater_can_be_invited(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient, gone: str
) -> None:
    outsider = make_actor(groups=frozenset())
    if gone == "removed":
        weave.remove_user(outsider.user.weave_sub)
    else:
        weave.set_active(outsider.user.weave_sub, False)
    project = _project(db_session, member)

    with pytest.raises(ValidationFailed):
        teams.invite(db_session, member, weave, project=project, query=outsider.user.weave_sub)


def test_the_lead_and_existing_members_cant_be_invited_twice(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    teammate = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)
    teams.invite(db_session, member, weave, project=project, query=teammate.user.weave_sub)

    for query, message in ((member.user.weave_sub, "lead"), (teammate.user.weave_sub, "invited already")):
        with pytest.raises(ValidationFailed) as exc:
            teams.invite(db_session, member, weave, project=project, query=query)
        assert message in exc.value.errors["member"]


def test_outsiders_cant_invite_and_closed_projects_cant_change(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    outsider = make_actor(groups=frozenset({GROUP_MEMBER}))
    newcomer = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)

    with pytest.raises(NotAllowed):
        teams.invite(db_session, outsider, weave, project=project, query=newcomer.user.weave_sub)

    project.status = ProjectStatus.COMPLETED
    with pytest.raises(InvalidState):
        teams.invite(db_session, member, weave, project=project, query=newcomer.user.weave_sub)


def test_only_the_invitee_answers_an_invitation(db_session: Session, member: Actor, make_actor, weave) -> None:
    invitee = make_actor(groups=frozenset({GROUP_MEMBER}))
    someone_else = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)
    invitation = add_to_team(db_session, project, invitee, accepted=False)

    with pytest.raises(NotFound):
        teams.accept(db_session, someone_else, weave, member_id=invitation.id)
    with pytest.raises(NotFound):
        teams.decline(db_session, member, member_id=invitation.id)

    teams.decline(db_session, invitee, member_id=invitation.id)
    assert project.members == []
    assert not teams.has_conflict(project, invitee.user.id)


def test_the_lead_removes_anyone_members_only_leave(db_session: Session, member: Actor, make_actor) -> None:
    first = make_actor(groups=frozenset({GROUP_MEMBER}))
    second = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)
    first_row = add_to_team(db_session, project, first)
    second_row = add_to_team(db_session, project, second)

    with pytest.raises(NotAllowed):
        teams.remove(db_session, first, project=project, member_id=second_row.id)

    teams.remove(db_session, first, project=project, member_id=first_row.id)
    teams.remove(db_session, member, project=project, member_id=second_row.id)

    assert project.members == []
    assert teams.team_user_ids(project) == [member.user.id]


def test_my_projects_lists_joined_teams_and_invitations_separately(
    db_session: Session, member: Actor, make_actor
) -> None:
    joined = make_actor(groups=frozenset({GROUP_MEMBER}))
    lead_of_other = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)
    other = _project(db_session, lead_of_other)
    add_to_team(db_session, project, joined)
    invitation = add_to_team(db_session, other, joined, accepted=False)

    assert [p.id for p in projects.list_projects_for_user(db_session, user_id=joined.user.id)] == [project.id]
    assert teams.pending_invitations(db_session, joined.user.id) == [invitation]


def test_an_approval_from_a_reviewer_who_later_joins_the_team_stops_counting(
    db_session: Session, member: Actor, make_actor, weave
) -> None:
    # Two approvals needed, so the first one leaves the revision pending.
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    first = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))
    second = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))
    project = projects.submit(db_session, member, project=_project(db_session, member), criteria_read=True)
    revision = project.current_revision
    projects.record_review(
        db_session,
        first,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    add_to_team(db_session, project, first, accepted=False)

    projects.record_review(
        db_session,
        second,
        revision=revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )

    assert revision.outcome is RevisionOutcome.PENDING
    assert "1 more approval" in approval_policy.explain(db_session, revision)


def test_nobody_leaves_or_is_removed_with_a_job_still_running(db_session: Session, member: Actor, make_actor) -> None:
    teammate = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _project(db_session, member)
    row = add_to_team(db_session, project, teammate)
    job = ComputeJob(
        project_id=project.id,
        submitter_id=teammate.user.id,
        kind=ComputeJobKind.BOX,
        name="box",
        image="ubuntu",
        gpu_names=["RTX4090"],
        disk_gb=32,
        target_microusd=100_000,
        max_microusd=200_000,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.KEEP_WAITING,
        max_runtime_minutes=60,
        state=ComputeJobState.STOPPED,
        queued_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.flush()

    for actor in (teammate, member):
        with pytest.raises(InvalidState, match="hasn't finished"):
            teams.remove(db_session, actor, project=project, member_id=row.id)

    job.state = ComputeJobState.CANCELLED
    teams.remove(db_session, member, project=project, member_id=row.id)
    assert project.members == []


def test_joining_a_funded_project_needs_a_minors_id_verification_to_pass(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    # Someone joining a project that already has budget could reach a running machine, so they are checked on accept.
    minor = make_actor(groups=frozenset({GROUP_MEMBER}))
    weave.set_minor(minor.user.weave_sub, True)
    weave.set_verification_status(minor.user.weave_sub, "pending")
    project = _project(db_session, member)
    draft = teams.invite(db_session, member, weave, project=project, query=minor.user.weave_sub)
    teams.accept(db_session, minor, weave, member_id=draft.id)  # no budget yet: compute checks them
    teams.remove(db_session, member, project=project, member_id=draft.id)
    budget_service.add_entry(
        db_session, project=project, kind=BudgetEntryKind.ADMIN_ADJUSTMENT, amount_cents=500, actor=member, reason="t"
    )
    invitation = teams.invite(db_session, member, weave, project=project, query=minor.user.weave_sub)

    with pytest.raises(VerificationRequired, match="You can't join this team yet"):
        teams.accept(db_session, minor, weave, member_id=invitation.id)
    assert not invitation.accepted

    weave.set_verification_status(minor.user.weave_sub, "verified")  # with the guardian on the call
    teams.accept(db_session, minor, weave, member_id=invitation.id)
    assert invitation.accepted


def test_someone_without_id_verification_cant_join_a_project_with_budget(
    db_session: Session, member: Actor, make_actor, weave: StubWeaveClient
) -> None:
    joiner = make_actor(groups=frozenset({GROUP_MEMBER}))
    weave.set_verification_status(joiner.user.weave_sub, "pending")
    project = _project(db_session, member)
    budget_service.add_entry(
        db_session, project=project, kind=BudgetEntryKind.ADMIN_ADJUSTMENT, amount_cents=500, actor=member, reason="t"
    )
    invitation = teams.invite(db_session, member, weave, project=project, query=joiner.user.weave_sub)

    with pytest.raises(VerificationRequired) as exc_info:
        teams.accept(db_session, joiner, weave, member_id=invitation.id)

    assert exc_info.value.members[0][1] == "ID verification in Weave is still in review"
    assert not invitation.accepted
