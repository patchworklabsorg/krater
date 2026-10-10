"""Tests for `krater.services.budget`: ledger arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from krater.models import BudgetEntryKind, Project, ProjectStatus, SpendSnapshot, SpendSource
from krater.services import budget
from krater.services.actor import Actor


def _make_project(db_session: Session, member: Actor) -> Project:
    project = Project(title="Budget Project", submitter_id=member.user.id, status=ProjectStatus.APPROVED)
    db_session.add(project)
    db_session.flush()
    return project


def test_ceiling_cents_is_zero_with_no_entries(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    assert budget.ceiling_cents(db_session, project) == 0


def test_ceiling_cents_sums_signed_entries(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=10_000, actor=member
    )
    budget.add_entry(db_session, project=project, kind=BudgetEntryKind.AMENDMENT, amount_cents=-2_000, actor=member)
    budget.add_entry(db_session, project=project, kind=BudgetEntryKind.ADMIN_ADJUSTMENT, amount_cents=500, actor=member)

    assert budget.ceiling_cents(db_session, project) == 8_500


def test_latest_spend_cents_is_zero_with_no_snapshots(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    assert budget.latest_spend_cents(db_session, project) == 0


def test_latest_spend_cents_returns_the_most_recent_snapshot(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)
    # Explicit times: Python 3.12's clock on Windows ticks every ~15ms, so two snapshots stamped
    # back to back can tie there and come back in either order.
    earlier = datetime.now(UTC) - timedelta(minutes=5)

    db_session.add(
        SpendSnapshot(
            project_id=project.id, estimated_spend_cents=100, source=SpendSource.SKYPILOT_COST_REPORT, taken_at=earlier
        )
    )
    db_session.flush()
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=300, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    assert budget.latest_spend_cents(db_session, project) == 300


def test_remaining_cents_is_ceiling_minus_spend(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=10_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=4_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    assert budget.remaining_cents(db_session, project) == 6_000
