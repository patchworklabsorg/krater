"""Granting less than a proposal or amendment asks for (`projects.record_review` / `admin_decide` with
`budget_cents`)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    AuditEvent,
    BudgetEntry,
    BudgetEntryKind,
    Project,
    ProjectStatus,
    ReviewDecision,
    ReviewSource,
)
from krater.services import budget, grants, projects
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import ValidationFailed
from tests.factories import new_project
from tests.services.conftest import add_to_team

REASON = "We fund a smaller first step, then more once it works."


def _submitted(db_session: Session, member: Actor, *, budget_cents: int = 18_000) -> Project:
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=budget_cents)
    return projects.submit(db_session, member, project=project, criteria_read=True)


def _approve(db_session, reviewer, project, weave, *, amount=None, reason=None, source=ReviewSource.WEB) -> Project:
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        reason=reason,
        source=source,
        weave_client=weave,
        budget_cents=amount,
    )
    db_session.refresh(project)
    return project


def _entries(db_session: Session, project: Project) -> list[BudgetEntry]:
    return list(db_session.scalars(sa.select(BudgetEntry).where(BudgetEntry.project_id == project.id)))


def test_a_reviewer_grants_less_than_the_proposal_asks_for(db_session, member, reviewer, weave) -> None:
    project = _approve(db_session, reviewer, _submitted(db_session, member), weave, amount=5_000, reason=REASON)

    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 5_000
    [entry] = _entries(db_session, project)
    assert (entry.kind, entry.amount_cents) == (BudgetEntryKind.INITIAL_APPROVAL, 5_000)
    revision = project.approved_revision
    assert (revision.budget_requested_cents, revision.budget_approved_cents) == (18_000, 5_000)
    assert revision.approved_budget_cents == 5_000
    assert revision.granted_less
    assert revision.reviews[0].budget_cents == 5_000
    assert revision.reviews[0].reason == REASON


def test_the_full_amount_or_none_grants_the_request(db_session, member, reviewer, make_actor, weave) -> None:
    project = _approve(db_session, reviewer, _submitted(db_session, member), weave, amount=18_000)

    assert budget.ceiling_cents(db_session, project) == 18_000
    assert project.approved_revision.budget_approved_cents is None
    assert not project.approved_revision.granted_less
    assert project.approved_revision.reviews[0].budget_cents is None


def test_a_held_grant_holds_the_lower_amount(db_session, member, make_actor, reviewer, weave) -> None:
    unverified = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Una Unverified")
    weave.set_minor(unverified.user.weave_sub, False)
    weave.set_verification_status(unverified.user.weave_sub, "needs_submission")
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=18_000)
    add_to_team(db_session, project, unverified)
    projects.submit(db_session, member, project=project, criteria_read=True)

    project = _approve(db_session, reviewer, project, weave, amount=5_000, reason=REASON)

    assert grants.pending(db_session, project).amount_cents == 5_000
    assert budget.ceiling_cents(db_session, project) == 0


def test_with_two_approvals_the_lowest_amount_wins(db_session, member, reviewer, make_actor, weave) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    second = make_actor(groups=frozenset({GROUP_MEMBER, "ganymede:reviewer"}), display_name="Second Reviewer")
    project = _approve(db_session, reviewer, _submitted(db_session, member), weave, amount=5_000, reason=REASON)
    assert project.status is ProjectStatus.PENDING_REVIEW

    # Slack's Approve button grants the full request, but the first reviewer's lower amount still stands.
    project = _approve(db_session, second, project, weave, source=ReviewSource.SLACK)

    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 5_000


def test_an_admin_override_grants_less_and_records_it(db_session, member, admin, weave) -> None:
    project = _submitted(db_session, member)

    projects.admin_decide(
        db_session,
        admin,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        reason=REASON,
        weave_client=weave,
        budget_cents=5_000,
    )

    assert budget.ceiling_cents(db_session, project) == 5_000
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == "admin_approve"))
    assert event.payload["budget_cents"] == 5_000
    assert event.payload["requested_cents"] == 18_000
    assert event.reason == REASON


@pytest.mark.parametrize(
    ("amount", "reason", "field"),
    [
        (0, REASON, "budget_cents"),
        (-100, REASON, "budget_cents"),
        (18_001, REASON, "budget_cents"),
        (5_000, None, "reason"),
        (5_000, "  ", "reason"),
    ],
)
def test_bad_amounts_and_a_missing_reason_are_refused(
    db_session, member, reviewer, weave, amount, reason, field
) -> None:
    project = _submitted(db_session, member)

    with pytest.raises(ValidationFailed) as excinfo:
        _approve(db_session, reviewer, project, weave, amount=amount, reason=reason)

    assert set(excinfo.value.errors) == {field}
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_an_amount_goes_only_with_an_approval(db_session, member, reviewer, weave) -> None:
    project = _submitted(db_session, member)

    with pytest.raises(ValidationFailed) as excinfo:
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.REJECT,
            reason="Not yet.",
            source=ReviewSource.WEB,
            weave_client=weave,
            budget_cents=5_000,
        )

    assert set(excinfo.value.errors) == {"budget_cents"}


def _amendment(db_session, member, reviewer, weave, *, new_total: int) -> Project:
    project = _approve(db_session, reviewer, _submitted(db_session, member, budget_cents=10_000), weave)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=new_total)
    return projects.submit(db_session, member, project=project, criteria_read=True)


def test_an_amendment_can_be_approved_for_a_lower_new_total(db_session, member, reviewer, weave) -> None:
    project = _amendment(db_session, member, reviewer, weave, new_total=30_000)

    project = _approve(db_session, reviewer, project, weave, amount=15_000, reason=REASON)

    assert budget.ceiling_cents(db_session, project) == 15_000
    amendment_entries = [e for e in _entries(db_session, project) if e.kind is BudgetEntryKind.AMENDMENT]
    assert [e.amount_cents for e in amendment_entries] == [5_000]
    assert project.approved_revision.budget_approved_cents == 15_000


def test_an_amendment_needs_a_new_total_above_todays_ceiling(db_session, member, reviewer, weave) -> None:
    project = _amendment(db_session, member, reviewer, weave, new_total=30_000)

    with pytest.raises(ValidationFailed) as excinfo:
        _approve(db_session, reviewer, project, weave, amount=10_000, reason=REASON)

    assert set(excinfo.value.errors) == {"budget_cents"}
    assert "reject the amendment" in excinfo.value.errors["budget_cents"]


def test_an_amendment_that_cuts_the_budget_is_approved_as_asked(db_session, member, reviewer, weave) -> None:
    project = _amendment(db_session, member, reviewer, weave, new_total=6_000)

    with pytest.raises(ValidationFailed) as excinfo:
        _approve(db_session, reviewer, project, weave, amount=5_000, reason=REASON)

    assert set(excinfo.value.errors) == {"budget_cents"}
