"""A smoke test for the ORM layer: create a user, a project, two revisions and a budget entry, then
read them all back through a fresh query, including relationships."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import (
    BudgetEntry,
    BudgetEntryKind,
    Project,
    ProjectRevision,
    ProjectStatus,
    RevisionKind,
    RevisionOutcome,
    User,
)


def test_create_project_with_revisions_and_budget_entry(db_session: Session) -> None:
    user = User(weave_sub="PWL0000TEST", display_name="Ada Lovelace", email="ada@example.com")
    db_session.add(user)
    db_session.flush()

    project = Project(title="Analytical Engine", submitter_id=user.id, status=ProjectStatus.DRAFT)
    db_session.add(project)
    db_session.flush()

    revision_1 = ProjectRevision(
        project_id=project.id,
        number=1,
        kind=RevisionKind.PROPOSAL,
        write_up="An engine for general computation.",
        budget_requested_cents=100_000,
        outcome=RevisionOutcome.SUPERSEDED,
    )
    revision_2 = ProjectRevision(
        project_id=project.id,
        number=2,
        kind=RevisionKind.PROPOSAL,
        write_up="An engine for general computation, revised.",
        budget_requested_cents=150_000,
        outcome=RevisionOutcome.APPROVED,
    )
    db_session.add_all([revision_1, revision_2])
    db_session.flush()

    # Only revision 2's approval counts: revision 1 was superseded by the resubmission.
    project.current_revision_id = revision_2.id
    project.approved_revision_id = revision_2.id
    project.status = ProjectStatus.APPROVED

    budget_entry = BudgetEntry(
        project_id=project.id,
        kind=BudgetEntryKind.INITIAL_APPROVAL,
        amount_cents=150_000,
        actor_id=user.id,
        revision_id=revision_2.id,
    )
    db_session.add(budget_entry)
    db_session.commit()
    db_session.expire_all()

    fetched_project = db_session.get(Project, project.id)
    assert fetched_project is not None
    assert fetched_project.title == "Analytical Engine"
    assert fetched_project.status is ProjectStatus.APPROVED
    assert fetched_project.current_revision_id == revision_2.id
    assert fetched_project.approved_revision_id == revision_2.id
    assert fetched_project.submitter.email == "ada@example.com"

    revisions = (
        db_session.query(ProjectRevision).filter_by(project_id=project.id).order_by(ProjectRevision.number).all()
    )
    assert [r.number for r in revisions] == [1, 2]
    assert revisions[0].outcome is RevisionOutcome.SUPERSEDED
    assert revisions[1].outcome is RevisionOutcome.APPROVED
    assert revisions[1].budget_requested_cents == 150_000

    fetched_entry = db_session.get(BudgetEntry, budget_entry.id)
    assert fetched_entry is not None
    assert fetched_entry.amount_cents == 150_000
    assert fetched_entry.kind is BudgetEntryKind.INITIAL_APPROVAL
    assert fetched_entry.actor.weave_sub == "PWL0000TEST"
    assert fetched_entry.revision_id == revision_2.id
