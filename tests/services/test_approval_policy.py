"""Tests for `krater.services.approval_policy`."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    Project,
    ProjectRevision,
    ProjectStatus,
    Review,
    ReviewDecision,
    ReviewSource,
    RevisionKind,
    RevisionOutcome,
)
from krater.services import approval_policy
from krater.services.actor import Actor


def _make_revision(
    db_session: Session,
    submitter: Actor,
    *,
    kind: RevisionKind = RevisionKind.PROPOSAL,
    budget_requested_cents: int = 1_000,
) -> ProjectRevision:
    project = Project(title="Policy Project", submitter_id=submitter.user.id, status=ProjectStatus.PENDING_REVIEW)
    db_session.add(project)
    db_session.flush()
    revision = ProjectRevision(
        project_id=project.id,
        number=1,
        kind=kind,
        write_up="write-up",
        budget_requested_cents=budget_requested_cents,
        submitted_at=None,
        outcome=RevisionOutcome.PENDING,
    )
    db_session.add(revision)
    db_session.flush()
    project.current_revision_id = revision.id
    db_session.flush()
    return revision


def _approve(db_session: Session, revision: ProjectRevision, reviewer: Actor, *, groups: frozenset[str]) -> None:
    db_session.add(
        Review(
            revision_id=revision.id,
            reviewer_id=reviewer.user.id,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
            reviewer_groups=sorted(groups),
        )
    )
    db_session.flush()


def test_stage_for_proposal_and_amendment_is_proposal_stage(db_session: Session, member: Actor) -> None:
    proposal = _make_revision(db_session, member, kind=RevisionKind.PROPOSAL)
    amendment = _make_revision(db_session, member, kind=RevisionKind.AMENDMENT)

    assert approval_policy.stage_for(proposal) is ApprovalStage.PROPOSAL
    assert approval_policy.stage_for(amendment) is ApprovalStage.PROPOSAL


def test_stage_for_completion_is_completion_stage(db_session: Session, member: Actor) -> None:
    completion = _make_revision(db_session, member, kind=RevisionKind.COMPLETION)

    assert approval_policy.stage_for(completion) is ApprovalStage.COMPLETION


def test_no_policy_rows_defaults_to_one_approval_from_any_reviewer(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    revision = _make_revision(db_session, member)

    assert approval_policy.applicable_policies(db_session, revision) == []
    assert approval_policy.is_satisfied(db_session, revision) is False

    _approve(db_session, revision, reviewer, groups=reviewer.groups)

    assert approval_policy.is_satisfied(db_session, revision) is True


def test_multi_approval_policy_requires_min_approvals(db_session: Session, member: Actor, make_actor) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    db_session.flush()
    revision = _make_revision(db_session, member)

    reviewer_a = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    reviewer_b = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))

    _approve(db_session, revision, reviewer_a, groups=reviewer_a.groups)
    assert approval_policy.is_satisfied(db_session, revision) is False

    _approve(db_session, revision, reviewer_b, groups=reviewer_b.groups)
    assert approval_policy.is_satisfied(db_session, revision) is True


def test_required_group_policy_only_counts_reviewers_in_that_group_at_review_time(
    db_session: Session, member: Actor, make_actor
) -> None:
    db_session.add(
        ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=1, required_group="ganymede:reviewer:senior")
    )
    db_session.flush()
    revision = _make_revision(db_session, member)

    plain_reviewer = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    senior_reviewer = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer", "ganymede:reviewer:senior"}))

    _approve(db_session, revision, plain_reviewer, groups=plain_reviewer.groups)
    assert approval_policy.is_satisfied(db_session, revision) is False

    _approve(db_session, revision, senior_reviewer, groups=senior_reviewer.groups)
    assert approval_policy.is_satisfied(db_session, revision) is True


def test_required_group_uses_snapshot_not_current_groups(db_session: Session, member: Actor, reviewer: Actor) -> None:
    """A reviewer's *current* groups changing later must not retroactively affect a past review."""
    db_session.add(
        ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=1, required_group="ganymede:reviewer:senior")
    )
    db_session.flush()
    revision = _make_revision(db_session, member)

    # Reviewed without the senior group; the policy shouldn't count it even if `reviewer.groups` is
    # (hypothetically) senior now -- what matters is what was snapshotted onto the Review row.
    _approve(db_session, revision, reviewer, groups=frozenset({"ganymede:member", "ganymede:reviewer"}))

    assert approval_policy.is_satisfied(db_session, revision) is False


def test_budget_tier_policy_only_applies_above_its_floor(db_session: Session, member: Actor) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=1))
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_budget_cents=100_000, min_approvals=2))
    db_session.flush()

    small = _make_revision(db_session, member, budget_requested_cents=1_000)
    large = _make_revision(db_session, member, budget_requested_cents=200_000)

    small_policies = approval_policy.applicable_policies(db_session, small)
    large_policies = approval_policy.applicable_policies(db_session, large)

    assert len(small_policies) == 1
    assert len(large_policies) == 2


def test_explain_reports_remaining_approvals(db_session: Session, member: Actor, reviewer: Actor) -> None:
    revision = _make_revision(db_session, member)

    assert approval_policy.explain(db_session, revision) == "Needs 1 more approval."

    _approve(db_session, revision, reviewer, groups=reviewer.groups)

    assert approval_policy.explain(db_session, revision) == "Policy satisfied."


def test_explain_names_the_required_group(db_session: Session, member: Actor) -> None:
    db_session.add(
        ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=1, required_group="ganymede:reviewer:senior")
    )
    db_session.flush()
    revision = _make_revision(db_session, member)

    assert "ganymede:reviewer:senior" in approval_policy.explain(db_session, revision)
