"""Progress updates and top-up requests (`krater.services.project_updates`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    BudgetEntry,
    BudgetEntryKind,
    ComputeEscalation,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    Project,
    ProjectStatus,
    ProjectUpdate,
    ReviewDecision,
    ReviewSource,
    RevisionKind,
    RevisionOutcome,
)
from krater.services import budget, project_updates, projects
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from tests.factories import new_project
from tests.services.conftest import add_to_team


def _job(db_session: Session, project: Project, member: Actor) -> ComputeJob:
    job = ComputeJob(
        project_id=project.id,
        submitter_id=member.user.id,
        kind=ComputeJobKind.JOB,
        name="train",
        image="ghcr.io/a/b:1",
        command="python t.py",
        gpu_names=["RTX 3060"],
        num_gpus=1,
        disk_gb=32,
        target_microusd=50_000,
        max_microusd=60_000,
        interruptible=True,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.KEEP_WAITING,
        max_runtime_minutes=60,
        state=ComputeJobState.SUCCEEDED,
        queued_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.flush()
    return job


def _approved(db_session: Session, member: Actor, reviewer: Actor, weave, *, cents: int = 5_000) -> Project:
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=cents)
    projects.submit(db_session, member, project=project, criteria_read=True)
    _decide(db_session, reviewer, project, weave)
    assert project.status is ProjectStatus.APPROVED
    return project


def _decide(db_session, reviewer, project, weave, decision=ReviewDecision.APPROVE, **extra) -> None:
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=decision,
        reason=extra.pop("reason", None if decision is ReviewDecision.APPROVE else "Not yet."),
        source=ReviewSource.WEB,
        weave_client=weave,
        **extra,
    )
    db_session.refresh(project)


def _topup(db_session, member, project, *, amount=2_500, reason="Two more training runs on an RTX 4090.", **extra):
    return project_updates.request_topup(
        db_session,
        member,
        project=project,
        amount_cents=amount,
        reason=reason,
        body=extra.pop("body", "The first model trains and reaches 80% accuracy."),
        criteria_read=extra.pop("criteria_read", True),
    )


def test_the_team_posts_an_update_with_the_budget_as_it_was(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _job(db_session, project, member)

    update = project_updates.post(db_session, member, project=project, body="  Trained the first model.  ")

    assert update.body == "Trained the first model."
    assert (update.ceiling_cents, update.spent_cents, update.jobs_since) == (5_000, 0, 1)
    assert not update.is_topup
    [view] = project_updates.list_for(db_session, member, project)
    assert view.update is update
    assert view.percent_used == 0


def test_jobs_since_counts_only_jobs_after_the_previous_update(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _job(db_session, project, member)
    first = project_updates.post(db_session, member, project=project, body="One.")
    db_session.execute(sa.update(ComputeJob).values(created_at=first.created_at - timedelta(minutes=1)))

    second = project_updates.post(db_session, member, project=project, body="Two.")

    assert second.jobs_since == 0


def test_an_update_needs_text_and_a_short_enough_one(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    with pytest.raises(ValidationFailed) as caught:
        project_updates.post(db_session, member, project=project, body="   ")
    assert "body" in caught.value.errors
    with pytest.raises(ValidationFailed):
        project_updates.post(db_session, member, project=project, body="x" * (project_updates.BODY_MAX_CHARS + 1))


def test_only_the_team_posts(db_session, member, reviewer, admin, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    for outsider in (reviewer, admin):
        with pytest.raises(NotAllowed):
            project_updates.post(db_session, outsider, project=project, body="Hi.")
        assert not project_updates.can_post(outsider, project)


def test_reviewers_and_admins_read_but_others_dont(db_session, member, reviewer, admin, make_actor, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    project_updates.post(db_session, member, project=project, body="Progress.")
    stranger = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Sam Stranger")

    assert len(project_updates.list_for(db_session, reviewer, project)) == 1
    assert len(project_updates.list_for(db_session, admin, project)) == 1
    with pytest.raises(NotAllowed):
        project_updates.list_for(db_session, stranger, project)


def test_no_updates_before_approval(db_session, member) -> None:
    project = new_project(db_session, member, title="Rover", write_up="A rover.")
    assert not project_updates.can_post(member, project)
    with pytest.raises(InvalidState):
        project_updates.post(db_session, member, project=project, body="Early.")


def test_a_topup_submits_an_amendment_for_the_ceiling_plus_the_amount(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    approved = project.approved_revision

    update = _topup(db_session, member, project)

    revision = update.revision
    assert revision is project.current_revision
    assert revision.kind is RevisionKind.AMENDMENT
    assert revision.submitted_at is not None
    assert revision.outcome is RevisionOutcome.PENDING
    assert revision.budget_requested_cents == 7_500
    assert revision.write_up == approved.write_up  # the application is unchanged
    assert revision.topup is update
    assert project_updates.for_revision(db_session, revision) is update
    assert (update.topup_cents, update.topup_reason) == (2_500, "Two more training runs on an RTX 4090.")
    assert project.status is ProjectStatus.APPROVED  # the budget keeps working meanwhile
    assert budget.ceiling_cents(db_session, project) == 5_000


def test_approving_a_topup_adds_it_to_the_ledger(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _topup(db_session, member, project)

    _decide(db_session, reviewer, project, weave)

    assert budget.ceiling_cents(db_session, project) == 7_500
    kinds = db_session.scalars(sa.select(BudgetEntry.kind).where(BudgetEntry.project_id == project.id)).all()
    assert BudgetEntryKind.AMENDMENT in kinds


def test_a_reviewer_can_grant_part_of_a_topup(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _topup(db_session, member, project)

    _decide(db_session, reviewer, project, weave, budget_cents=6_000, reason="One run first.")

    assert budget.ceiling_cents(db_session, project) == 6_000


def test_a_rejected_topup_changes_nothing(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    update = _topup(db_session, member, project)

    _decide(db_session, reviewer, project, weave, decision=ReviewDecision.REJECT)

    assert update.revision.outcome is RevisionOutcome.REJECTED
    assert budget.ceiling_cents(db_session, project) == 5_000
    assert project.status is ProjectStatus.APPROVED
    assert project_updates.can_request_topup(project)  # they can ask again


def test_a_topup_needs_an_amount_a_reason_and_the_criteria_box(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    with pytest.raises(ValidationFailed) as caught:
        _topup(db_session, member, project, amount=None, reason=" ", criteria_read=False, body="")
    assert set(caught.value.errors) == {"amount", "reason", "criteria_read"}
    with pytest.raises(ValidationFailed) as caught:
        _topup(db_session, member, project, amount=project_updates.TOPUP_MAX_CENTS + 1)
    assert "amount" in caught.value.errors
    assert db_session.scalar(sa.select(sa.func.count()).select_from(ProjectUpdate)) == 0


def test_a_topup_without_progress_is_allowed(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    update = _topup(db_session, member, project, body="")
    assert update.body == ""
    assert update.revision is not None


def test_one_topup_at_a_time(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    first = _topup(db_session, member, project)

    assert not project_updates.can_request_topup(project)
    with pytest.raises(InvalidState):
        _topup(db_session, member, project, amount=1_000)
    assert first.revision.outcome is RevisionOutcome.PENDING

    _decide(db_session, reviewer, project, weave)
    assert project_updates.can_request_topup(project)


def test_no_full_amendment_while_a_topup_is_in_review(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _topup(db_session, member, project)

    with pytest.raises(InvalidState):
        projects.start_amendment(db_session, member, project=project)


def test_no_topup_while_an_amendment_draft_is_open(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    projects.start_amendment(db_session, member, project=project)

    assert not project_updates.can_request_topup(project)
    with pytest.raises(InvalidState):
        _topup(db_session, member, project)


def test_no_topup_while_a_grant_is_held(db_session, member, reviewer, make_actor, weave) -> None:
    unverified = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Una Unverified")
    weave.set_minor(unverified.user.weave_sub, False)
    weave.set_verification_status(unverified.user.weave_sub, "needs_submission")
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=5_000)
    add_to_team(db_session, project, unverified)
    projects.submit(db_session, member, project=project, criteria_read=True)
    _decide(db_session, reviewer, project, weave)

    with pytest.raises(InvalidState):
        _topup(db_session, member, project)


def test_an_application_missing_new_answers_points_to_a_full_amendment(db_session, member, reviewer, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    project.approved_revision.training_data = ""  # a question added after it was approved
    db_session.flush()

    with pytest.raises(ValidationFailed) as caught:
        _topup(db_session, member, project)

    assert "full amendment" in caught.value.errors["application"]
    assert "training_data" in caught.value.errors["application"]
