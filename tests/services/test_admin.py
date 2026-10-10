"""Admin overrides: bypassing policy/self-review, budget adjustments, reclaims, audit, and withdraw."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    AuditEvent,
    BudgetEntryKind,
    ProjectStatus,
    ReviewDecision,
    RevisionOutcome,
    SpendSnapshot,
    SpendSource,
)
from krater.services import budget, projects
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed


def _submitted_project(db_session: Session, member: Actor, *, budget_requested_cents: int = 10_000):
    project = projects.create_project(
        db_session, member, title="Admin Test", write_up="Write-up.", budget_requested_cents=budget_requested_cents
    )
    return projects.submit(db_session, member, project=project)


def _approved_project(db_session: Session, member: Actor, admin: Actor, *, budget_requested_cents: int = 10_000):
    project = _submitted_project(db_session, member, budget_requested_cents=budget_requested_cents)
    projects.admin_decide(
        db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="ok"
    )
    db_session.refresh(project)
    return project


def test_admin_decide_bypasses_policy_and_approves_directly(db_session: Session, member: Actor, admin: Actor) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=5))
    db_session.flush()
    project = _submitted_project(db_session, member)

    revision = projects.admin_decide(
        db_session,
        admin,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        reason="Admin override: looks fine.",
    )

    assert revision.outcome is RevisionOutcome.APPROVED
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert project.approved_revision_id == revision.id

    event = db_session.query(AuditEvent).filter_by(project_id=project.id, action="admin_approve").one()
    assert event.reason == "Admin override: looks fine."
    assert event.actor_id == admin.user.id


@pytest.mark.parametrize("decision", [ReviewDecision.APPROVE, ReviewDecision.REJECT])
def test_an_admin_cannot_decide_their_own_project(db_session: Session, member: Actor, decision: ReviewDecision) -> None:
    """Otherwise an admin could approve, and so fund, their own proposal with nobody else signing off."""
    submitter_admin = Actor(user=member.user, groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    project = _submitted_project(db_session, submitter_admin)

    with pytest.raises(NotAllowed, match="another admin"):
        projects.admin_decide(
            db_session, submitter_admin, revision=project.current_revision, decision=decision, reason="Mine."
        )

    assert project.current_revision.outcome is RevisionOutcome.PENDING


def test_an_admin_cannot_decide_a_project_that_credits_them(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _submitted_project(db_session, member)
    project.current_revision.credited_builder_ids = [admin.user.id]
    db_session.flush()

    with pytest.raises(NotAllowed):
        projects.admin_decide(
            db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="x"
        )


def test_admin_decide_reject_opens_next_draft_and_audits(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _submitted_project(db_session, member)
    revision = project.current_revision

    projects.admin_decide(db_session, admin, revision=revision, decision=ReviewDecision.REJECT, reason="Not yet.")

    db_session.refresh(project)
    assert project.status is ProjectStatus.CHANGES_REQUESTED
    assert revision.outcome is RevisionOutcome.REJECTED
    assert project.current_revision.id != revision.id

    event = db_session.query(AuditEvent).filter_by(project_id=project.id, action="admin_reject").one()
    assert event.reason == "Not yet."


def test_admin_decide_requires_a_reason(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _submitted_project(db_session, member)

    with pytest.raises(ValidationFailed):
        projects.admin_decide(
            db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason=""
        )


def test_admin_decide_requires_admin(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)

    with pytest.raises(NotAllowed):
        projects.admin_decide(
            db_session, reviewer, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="x"
        )


def test_admin_adjust_budget_up_and_down(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=10_000)

    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=5_000, reason="Extra credit.")
    assert budget.ceiling_cents(db_session, project) == 15_000

    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=-3_000, reason="Trim.")
    assert budget.ceiling_cents(db_session, project) == 12_000

    events = db_session.query(AuditEvent).filter_by(project_id=project.id, action="admin_adjust_budget").all()
    assert len(events) == 2


def test_admin_adjust_budget_cannot_take_ceiling_below_zero(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=1_000)

    with pytest.raises(ValidationFailed):
        projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=-2_000, reason="Too much.")

    assert budget.ceiling_cents(db_session, project) == 1_000


def test_admin_adjust_budget_only_on_approved_or_pending_completion(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    project = _submitted_project(db_session, member)  # status: pending_review

    with pytest.raises(InvalidState):
        projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=100, reason="x")


def test_admin_adjust_budget_requires_reason(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin)

    with pytest.raises(ValidationFailed):
        projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=100, reason="   ")


def test_admin_adjust_budget_requires_admin(db_session: Session, member: Actor, admin: Actor, reviewer: Actor) -> None:
    project = _approved_project(db_session, member, admin)

    with pytest.raises(NotAllowed):
        projects.admin_adjust_budget(db_session, reviewer, project=project, amount_cents=100, reason="x")


def test_an_admin_cannot_add_budget_to_their_own_project_but_can_cut_it(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    submitter_admin = Actor(user=member.user, groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    project = _approved_project(db_session, submitter_admin, admin, budget_requested_cents=10_000)

    with pytest.raises(NotAllowed):
        projects.admin_adjust_budget(db_session, submitter_admin, project=project, amount_cents=5_000, reason="More.")
    projects.admin_adjust_budget(db_session, submitter_admin, project=project, amount_cents=-2_000, reason="Less.")

    assert budget.ceiling_cents(db_session, project) == 8_000


def test_reclaim_budget_reduces_ceiling(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=10_000)

    entry = projects.reclaim_budget(db_session, admin, project=project, amount_cents=4_000, reason="Stalled.")

    assert entry.kind is BudgetEntryKind.RECLAIM
    assert entry.amount_cents == -4_000
    assert budget.ceiling_cents(db_session, project) == 6_000

    event = db_session.query(AuditEvent).filter_by(project_id=project.id, action="admin_reclaim_budget").one()
    assert event.reason == "Stalled."


def test_reclaim_budget_amount_must_be_positive(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=10_000)

    with pytest.raises(ValidationFailed):
        projects.reclaim_budget(db_session, admin, project=project, amount_cents=0, reason="x")


def test_reclaim_budget_cannot_exceed_ceiling(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=1_000)

    with pytest.raises(ValidationFailed):
        projects.reclaim_budget(db_session, admin, project=project, amount_cents=5_000, reason="x")


def test_withdraw_by_submitter_reclaims_remaining_budget_without_audit(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=10_000)
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=2_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    projects.withdraw(db_session, member, project=project)

    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN
    assert budget.ceiling_cents(db_session, project) == 2_000
    assert budget.remaining_cents(db_session, project) == 0
    # No withdrawal-related audit event: only the (unrelated) admin_approve from `_approved_project`.
    assert db_session.query(AuditEvent).filter_by(project_id=project.id, action="admin_withdraw").count() == 0


def test_withdraw_by_admin_writes_audit_and_requires_reason(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin, budget_requested_cents=5_000)

    with pytest.raises(ValidationFailed):
        projects.withdraw(db_session, admin, project=project, reason="")

    projects.withdraw(db_session, admin, project=project, reason="Inactive for 6 months.")
    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN
    event = db_session.query(AuditEvent).filter_by(project_id=project.id, action="admin_withdraw").one()
    assert event.reason == "Inactive for 6 months."
    assert event.payload["reclaimed_cents"] == 5_000


def test_withdraw_requires_submitter_or_admin(
    db_session: Session, member: Actor, admin: Actor, reviewer: Actor
) -> None:
    project = _approved_project(db_session, member, admin)

    with pytest.raises(NotAllowed):
        projects.withdraw(db_session, reviewer, project=project, reason="Not yours.")


def test_withdraw_from_a_terminal_state_is_invalid(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _approved_project(db_session, member, admin)
    projects.withdraw(db_session, member, project=project)
    db_session.refresh(project)

    with pytest.raises(InvalidState):
        projects.withdraw(db_session, member, project=project)
