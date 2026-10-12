"""The full happy path: proposal -> approve -> amend (decrease) -> approve -> completion -> approve
(reclaim). One end-to-end test per SPEC's "at minimum" list, exercising the budget ledger throughout.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import (
    BudgetEntryKind,
    ProjectStatus,
    ReviewDecision,
    ReviewSource,
    RevisionKind,
    RevisionOutcome,
    SpendSnapshot,
    SpendSource,
)
from krater.services import budget, projects
from krater.services.actor import Actor


def test_happy_path_proposal_amendment_and_completion(db_session: Session, member: Actor, reviewer: Actor) -> None:
    # --- Draft & submit the proposal --------------------------------------------------------
    project = projects.create_project(db_session, member, title="", write_up="", budget_requested_cents=0)
    assert project.status is ProjectStatus.DRAFT
    assert project.current_revision.number == 1
    assert project.current_revision.kind is RevisionKind.PROPOSAL
    assert project.current_revision.submitted_at is None

    projects.update_draft(
        db_session,
        member,
        project=project,
        title="Ganymede Rover",
        repo_url="https://github.com/patchworklabs/rover",
        write_up="A rover for Ganymede.",
        budget_requested_cents=100_000,
    )

    project = projects.submit(db_session, member, project=project)
    assert project.status is ProjectStatus.PENDING_REVIEW
    proposal_revision = project.current_revision
    assert proposal_revision.submitted_at is not None
    assert proposal_revision.outcome is RevisionOutcome.PENDING

    # --- Review & approve the proposal (default policy: 1 approval from any reviewer) -------
    review = projects.record_review(
        db_session,
        reviewer,
        revision=proposal_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )
    assert review.reviewer_groups == sorted(reviewer.groups)

    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert project.approved_revision_id == proposal_revision.id
    assert budget.ceiling_cents(db_session, project) == 100_000

    entries = {e.kind for e in project.budget_entries}
    assert entries == {BudgetEntryKind.INITIAL_APPROVAL}

    # --- Amend: decrease the budget -----------------------------------------------------------
    amendment = projects.start_amendment(db_session, member, project=project)
    assert amendment.kind is RevisionKind.AMENDMENT
    assert amendment.budget_requested_cents == 100_000  # copied from the approved revision

    projects.update_draft(db_session, member, project=project, budget_requested_cents=60_000)
    project = projects.submit(db_session, member, project=project)
    # Amendments leave the project approved while under review.
    assert project.status is ProjectStatus.APPROVED
    assert amendment.submitted_at is not None

    projects.record_review(
        db_session, reviewer, revision=amendment, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert project.approved_revision_id == amendment.id
    # Ceiling decreased by 40,000: a BudgetEntry(amendment, -40_000) was written.
    assert budget.ceiling_cents(db_session, project) == 60_000
    amendment_entry = next(e for e in project.budget_entries if e.kind is BudgetEntryKind.AMENDMENT)
    assert amendment_entry.amount_cents == -40_000

    # --- Completion, with some spend to reclaim -----------------------------------------------
    completion = projects.start_completion(db_session, member, project=project)
    assert completion.kind is RevisionKind.COMPLETION

    projects.update_draft(
        db_session,
        member,
        project=project,
        demo_url="https://example.com/demo",
        tags=["rover", "ganymede"],
    )
    project = projects.submit_completion(db_session, member, project=project)
    assert project.status is ProjectStatus.PENDING_COMPLETION_REVIEW
    assert completion.submitted_at is not None

    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=35_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    projects.record_review(
        db_session, reviewer, revision=completion, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.COMPLETED
    assert completion.outcome is RevisionOutcome.APPROVED

    # Ceiling was 60,000, spend was 35,000: 25,000 unspent should have been reclaimed, leaving the
    # ceiling equal to spend (remaining == 0).
    assert budget.ceiling_cents(db_session, project) == 35_000
    assert budget.remaining_cents(db_session, project) == 0
    reclaim_entry = next(e for e in project.budget_entries if e.kind is BudgetEntryKind.RECLAIM)
    assert reclaim_entry.amount_cents == -25_000
