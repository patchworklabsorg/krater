"""Funding holds: approving a project as awaiting funding or a 3rd party review (`krater.services.funding_holds`,
docs/SPEC.md "Funding holds")."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    AuditEvent,
    BudgetEntry,
    FundingHold,
    HeldGrant,
    Project,
    ProjectStatus,
    QuiltOutbox,
    ReviewDecision,
    ReviewSource,
)
from krater.services import budget, compute_jobs, funding_holds, grants, projects
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.compute_jobs import JobSpec
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.services.quilt_events import BUDGET_COMMITTED
from krater.weave import StubWeaveClient
from tests.factories import new_project
from tests.services.conftest import add_to_team

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
FUNDING = FundingHold.AWAITING_FUNDING
THIRD_PARTY = FundingHold.AWAITING_THIRD_PARTY_REVIEW


def _submitted(db_session: Session, member: Actor, *, budget_cents: int = 10_000) -> Project:
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=budget_cents)
    return projects.submit(db_session, member, project=project, criteria_read=True)


def _review(db_session, reviewer, project, weave, *, hold=None, source=ReviewSource.WEB) -> Project:
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=source,
        weave_client=weave,
        funding_hold=hold,
    )
    db_session.refresh(project)
    return project


def _ledger(db_session: Session, project: Project) -> list[BudgetEntry]:
    return list(db_session.scalars(sa.select(BudgetEntry).where(BudgetEntry.project_id == project.id)))


def _committed(db_session: Session, project: Project) -> list[QuiltOutbox]:
    return list(
        db_session.scalars(
            sa.select(QuiltOutbox).where(
                QuiltOutbox.external_id == str(project.id), QuiltOutbox.type == BUDGET_COMMITTED
            )
        )
    )


@pytest.fixture
def unverified(make_actor, weave: StubWeaveClient) -> Actor:
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Una Unverified")
    weave.set_minor(actor.user.weave_sub, False)
    weave.set_verification_status(actor.user.weave_sub, "needs_submission")
    return actor


def test_an_approval_with_a_hold_counts_but_holds_the_budget_of_a_verified_team(
    db_session, member, reviewer, weave
) -> None:
    project = _review(db_session, reviewer, _submitted(db_session, member), weave, hold=FUNDING)

    assert project.status is ProjectStatus.APPROVED
    assert project.funding_hold is FUNDING
    assert _ledger(db_session, project) == []
    assert budget.ceiling_cents(db_session, project) == 0
    assert _committed(db_session, project) == []  # Quilt hears of the budget only once it's granted
    held = grants.pending(db_session, project)
    assert held.amount_cents == 10_000
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == grants.AUDIT_GRANT_HELD))
    assert event.payload["funding_hold"] == FUNDING.value


def test_the_worker_doesnt_release_a_held_project(db_session, member, reviewer, weave) -> None:
    project = _review(db_session, reviewer, _submitted(db_session, member), weave, hold=THIRD_PARTY)

    assert grants.release_ready(db_session, weave, now=NOW) == 0

    assert grants.pending(db_session, project) is not None
    assert _ledger(db_session, project) == []


def test_clearing_the_hold_releases_the_budget_when_the_team_is_verified(
    db_session, member, reviewer, admin, weave
) -> None:
    project = _review(db_session, reviewer, _submitted(db_session, member), weave, hold=FUNDING)

    released = funding_holds.set_hold(
        db_session, admin, project=project, hold=None, reason="Funded by the grant.", weave_client=weave, now=NOW
    )

    assert released is True
    assert project.funding_hold is None
    assert budget.ceiling_cents(db_session, project) == 10_000
    assert len(_committed(db_session, project)) == 1
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == funding_holds.AUDIT_CLEAR))
    assert event.reason == "Funded by the grant."
    assert event.payload == {"funding_hold": None, "previous": FUNDING.value}


def test_clearing_the_hold_leaves_the_verification_hold(db_session, member, unverified, reviewer, admin, weave) -> None:
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=10_000)
    add_to_team(db_session, project, unverified)
    projects.submit(db_session, member, project=project, criteria_read=True)
    project = _review(db_session, reviewer, project, weave, hold=FUNDING)

    released = funding_holds.set_hold(
        db_session, admin, project=project, hold=None, reason="Funded.", weave_client=weave, now=NOW
    )

    assert released is False
    assert grants.pending(db_session, project) is not None
    weave.set_verification_status(unverified.user.weave_sub, "verified")
    assert grants.release_ready(db_session, weave, now=NOW) == 1
    assert budget.ceiling_cents(db_session, project) == 10_000


def test_a_later_approval_from_slack_keeps_the_first_reviewers_hold(
    db_session, member, reviewer, make_actor, weave
) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    second = make_actor(groups=frozenset({GROUP_MEMBER, "ganymede:reviewer"}), display_name="Second Reviewer")
    project = _review(db_session, reviewer, _submitted(db_session, member), weave, hold=THIRD_PARTY)
    assert project.status is ProjectStatus.PENDING_REVIEW
    assert project.funding_hold is None  # not until the approval counts

    project = _review(db_session, second, project, weave, source=ReviewSource.SLACK)

    assert project.status is ProjectStatus.APPROVED
    assert project.funding_hold is THIRD_PARTY
    assert _ledger(db_session, project) == []


def test_an_admin_decision_can_hold_it(db_session, member, admin, weave) -> None:
    project = _submitted(db_session, member)

    projects.admin_decide(
        db_session,
        admin,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        reason="Approved at the meeting.",
        weave_client=weave,
        funding_hold=FUNDING,
    )

    assert project.funding_hold is FUNDING
    assert grants.pending(db_session, project) is not None
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == "admin_approve"))
    assert event.payload["funding_hold"] == FUNDING.value


def test_a_hold_goes_only_with_approving_a_proposal_or_amendment(db_session, member, reviewer, weave) -> None:
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
            funding_hold=FUNDING,
        )
    assert set(excinfo.value.errors) == {"funding_hold"}


def test_an_admin_can_hold_a_funded_project_later_and_compute_stops_starting(
    db_session, member, reviewer, admin, weave, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    project = _review(db_session, reviewer, _submitted(db_session, member), weave)
    assert budget.ceiling_cents(db_session, project) == 10_000

    funding_holds.set_hold(
        db_session, admin, project=project, hold=THIRD_PARTY, reason="OSPool review.", weave_client=weave, now=NOW
    )

    assert budget.ceiling_cents(db_session, project) == 10_000  # nothing granted is taken back
    spec = JobSpec(
        name="train",
        image="ghcr.io/alice/train:v3",
        command="python train.py",
        gpu_names=("RTX 3060",),
        target_microusd=70_000,
        max_microusd=100_000,
    )
    with pytest.raises(funding_holds.FundingHeld):
        compute_jobs.submit_job(
            db_session, member, project=project, spec=spec, cap_cents=500, now=NOW, weave_client=weave
        )
    # A budget increase meanwhile is held too.
    projects.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=5_000, reason="More.", weave_client=weave
    )
    assert budget.ceiling_cents(db_session, project) == 10_000
    with pytest.raises(InvalidState, match="funding hold"):
        grants.require_none_pending(db_session, project)


def test_only_admins_set_holds_and_not_to_clear_their_own(db_session, member, reviewer, admin, weave) -> None:
    project = _review(db_session, reviewer, _submitted(db_session, member), weave, hold=FUNDING)

    for actor in (member, reviewer):
        with pytest.raises(NotAllowed):
            funding_holds.set_hold(
                db_session, actor, project=project, hold=None, reason="x", weave_client=weave, now=NOW
            )
    add_to_team(db_session, project, admin)
    with pytest.raises(NotAllowed):
        funding_holds.set_hold(db_session, admin, project=project, hold=None, reason="x", weave_client=weave, now=NOW)
    # Changing the kind of hold on their own project funds nothing, so it's allowed.
    funding_holds.set_hold(
        db_session, admin, project=project, hold=THIRD_PARTY, reason="x", weave_client=weave, now=NOW
    )
    assert project.funding_hold is THIRD_PARTY


def test_a_hold_needs_a_reason_an_approved_project_and_a_change(db_session, member, reviewer, admin, weave) -> None:
    project = _submitted(db_session, member)
    with pytest.raises(ValidationFailed):
        funding_holds.set_hold(
            db_session, admin, project=project, hold=FUNDING, reason=" ", weave_client=weave, now=NOW
        )
    with pytest.raises(InvalidState):
        funding_holds.set_hold(
            db_session, admin, project=project, hold=FUNDING, reason="x", weave_client=weave, now=NOW
        )
    project = _review(db_session, reviewer, project, weave)
    with pytest.raises(InvalidState):
        funding_holds.set_hold(db_session, admin, project=project, hold=None, reason="x", weave_client=weave, now=NOW)


def test_withdrawing_a_held_project_cancels_its_hold(db_session, member, reviewer, weave) -> None:
    project = _review(db_session, reviewer, _submitted(db_session, member), weave, hold=FUNDING)

    projects.withdraw(db_session, member, project=project)

    assert db_session.scalar(sa.select(HeldGrant).where(HeldGrant.project_id == project.id)).cancelled_at is not None


def test_an_amendment_draft_asks_for_the_criteria_again(db_session, member, reviewer, weave) -> None:
    project = _review(db_session, reviewer, _submitted(db_session, member), weave)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=20_000)

    assert project.current_revision.criteria_read_at is None
    assert project.current_revision.training_data == "Cave scans we took ourselves."  # answers carry over
    with pytest.raises(ValidationFailed) as excinfo:
        projects.submit(db_session, member, project=project)
    assert set(excinfo.value.errors) == {"criteria_read"}
