"""Review recording: self-review, double review, non-reviewer, and reject/resubmit."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    Project,
    ProjectStatus,
    Review,
    ReviewDecision,
    ReviewSource,
    RevisionOutcome,
)
from krater.services import projects
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed


def _submitted_project(db_session: Session, member: Actor, *, budget_requested_cents: int = 10_000):
    project = projects.create_project(
        db_session,
        member,
        title="Test Project",
        write_up="Write-up.",
        budget_requested_cents=budget_requested_cents,
    )
    project = projects.submit(db_session, member, project=project)
    return project


def test_non_reviewer_cannot_record_a_review(db_session: Session, member: Actor, make_actor) -> None:
    project = _submitted_project(db_session, member)
    plain_member = make_actor(groups=frozenset({"ganymede:member"}))

    with pytest.raises(NotAllowed):
        projects.record_review(
            db_session,
            plain_member,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_submitter_cannot_review_their_own_submission(db_session: Session, make_actor) -> None:
    submitter_and_reviewer = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    project = _submitted_project(db_session, submitter_and_reviewer)

    with pytest.raises(NotAllowed):
        projects.record_review(
            db_session,
            submitter_and_reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_credited_builder_cannot_review(db_session: Session, member: Actor, make_actor) -> None:
    builder = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    project = _submitted_project(db_session, member)
    project.current_revision.credited_builder_ids = [builder.user.id]
    db_session.flush()

    with pytest.raises(NotAllowed):
        projects.record_review(
            db_session,
            builder,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_double_review_by_the_same_reviewer_is_blocked(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)
    # First approval alone would satisfy the default policy and close out the revision, so use a
    # multi-approval policy to keep it open for a second decision attempt.
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    db_session.flush()

    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_concurrent_double_review_is_blocked_by_the_db_constraint(
    db_session: Session, member: Actor, reviewer: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates two concurrent requests that both pass the `_has_existing_review` pre-check before
    either has inserted its row (e.g. a double-click, or a retried Slack action): the `reviews` unique
    constraint is what actually stops the second insert, and `record_review` must turn the resulting
    `IntegrityError` into the same `InvalidState` the pre-check normally raises.

    Mirrors how this plays out for real, across two separate requests/sessions: the first review is
    committed (as the router would on success) before the "concurrent" second attempt comes in, and a
    plain `session.rollback()` (as the router would do on an `InvalidState`) is enough to leave the
    session usable again -- it only discards the second attempt's own (already-failed) work.
    """
    # A multi-approval policy keeps the revision `pending` after the first approval, so there's still a
    # decision to (attempt to) record when the "concurrent" second request comes in.
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    db_session.flush()
    project = _submitted_project(db_session, member)

    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )
    db_session.commit()

    monkeypatch.setattr(projects, "_has_existing_review", lambda *args, **kwargs: False)
    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
    db_session.rollback()

    # The first (committed) review survived, and the session is usable again for a fresh query.
    reloaded = db_session.get(Project, project.id)
    assert reloaded is not None
    assert reloaded.status is ProjectStatus.PENDING_REVIEW
    review_count = db_session.scalar(
        sa.select(sa.func.count()).select_from(Review).where(Review.revision_id == project.current_revision_id)
    )
    assert review_count == 1


def test_reject_requires_a_reason(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)

    with pytest.raises(ValidationFailed):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.REJECT,
            source=ReviewSource.WEB,
        )


def test_a_single_reject_rejects_the_revision_immediately_and_opens_the_next_draft(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _submitted_project(db_session, member, budget_requested_cents=10_000)
    rejected_revision = project.current_revision

    projects.record_review(
        db_session,
        reviewer,
        revision=rejected_revision,
        decision=ReviewDecision.REJECT,
        reason="Needs more detail.",
        source=ReviewSource.WEB,
    )

    db_session.refresh(project)
    assert project.status is ProjectStatus.CHANGES_REQUESTED
    assert rejected_revision.outcome is RevisionOutcome.REJECTED

    new_draft = project.current_revision
    assert new_draft.id != rejected_revision.id
    assert new_draft.number == rejected_revision.number + 1
    assert new_draft.submitted_at is None
    assert new_draft.budget_requested_cents == 10_000


def test_resubmit_after_reject_does_not_count_old_reviews(db_session: Session, member: Actor, make_actor) -> None:
    """A reviewer's rejection of an earlier revision must not carry over to (or block approval of) the
    resubmitted one -- only the current revision's own reviews count toward the policy."""
    reviewer_a = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    reviewer_b = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))

    project = _submitted_project(db_session, member)
    first_revision = project.current_revision

    projects.record_review(
        db_session,
        reviewer_a,
        revision=first_revision,
        decision=ReviewDecision.REJECT,
        reason="Not ready.",
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.CHANGES_REQUESTED

    projects.update_draft(db_session, member, project=project, write_up="Revised write-up.")
    project = projects.submit(db_session, member, project=project)
    second_revision = project.current_revision
    assert second_revision.id != first_revision.id

    # The default policy (1 approval, any reviewer) is satisfied by reviewer_b's single approval, even
    # though reviewer_a already rejected the *first* revision.
    projects.record_review(
        db_session, reviewer_b, revision=second_revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert project.approved_revision_id == second_revision.id


def test_cannot_review_a_non_current_revision(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)
    old_revision = project.current_revision

    projects.record_review(
        db_session,
        reviewer,
        revision=old_revision,
        decision=ReviewDecision.REJECT,
        reason="Needs work.",
        source=ReviewSource.WEB,
    )

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=old_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_cannot_review_a_draft_revision(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="T", write_up="W", budget_requested_cents=1_000)

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_withdraw_supersedes_the_pending_revision_and_a_review_cannot_bring_it_back(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    """A withdrawn project's still-`pending` submitted revision must not be approvable back to life:
    `withdraw` has to mark it `superseded`, and `record_review` has to reject it regardless."""
    project = _submitted_project(db_session, member)
    pending_revision = project.current_revision

    projects.withdraw(db_session, member, project=project)
    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN
    db_session.refresh(pending_revision)
    assert pending_revision.outcome is RevisionOutcome.SUPERSEDED

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=pending_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN


def test_withdrawn_project_does_not_appear_in_the_review_queue(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _submitted_project(db_session, member)
    projects.withdraw(db_session, member, project=project)

    queue = projects.review_queue(db_session, reviewer)

    assert project.current_revision not in queue


def test_admin_decide_cannot_resurrect_a_withdrawn_project(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _submitted_project(db_session, member)
    pending_revision = project.current_revision
    projects.withdraw(db_session, member, project=project)

    with pytest.raises(InvalidState):
        projects.admin_decide(
            db_session, admin, revision=pending_revision, decision=ReviewDecision.APPROVE, reason="override"
        )
    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN


def test_cannot_review_a_completion_revision_while_project_is_not_in_completion_review(
    db_session: Session, member: Actor, admin: Actor, reviewer: Actor
) -> None:
    """A completion revision must only be decided while the project is `pending_completion_review` --
    e.g. not after an admin has withdrawn the project out from under an in-flight completion review."""
    project = _submitted_project(db_session, member)
    projects.admin_decide(
        db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="ok"
    )
    db_session.refresh(project)
    projects.start_completion(db_session, member, project=project)
    project = projects.submit_completion(db_session, member, project=project)
    completion_revision = project.current_revision
    assert project.status is ProjectStatus.PENDING_COMPLETION_REVIEW

    projects.withdraw(db_session, admin, project=project, reason="stopping the completion review")
    db_session.refresh(project)
    assert project.status is ProjectStatus.WITHDRAWN
    db_session.refresh(completion_revision)
    assert completion_revision.outcome is RevisionOutcome.SUPERSEDED

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=completion_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
