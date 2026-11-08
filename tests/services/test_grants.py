"""Budget grants held until Weave clears the team (`krater.services.grants`, docs/SPEC.md "Verification before
funding")."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    AuditEvent,
    BudgetEntry,
    BudgetEntryKind,
    HeldGrant,
    Project,
    ProjectStatus,
    QuiltOutbox,
    ReviewDecision,
    ReviewSource,
)
from krater.services import budget, grants, projects
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import InvalidState
from krater.services.quilt_events import BUDGET_COMMITTED
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.factories import new_project
from tests.services.conftest import add_to_team

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class _DownWeave:
    """A Weave that can't be reached."""

    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


@pytest.fixture
def unverified(make_actor, weave: StubWeaveClient) -> Actor:
    """An adult Ganymede member who hasn't done ID verification in Weave."""
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Una Unverified")
    weave.set_minor(actor.user.weave_sub, False)
    weave.set_verification_status(actor.user.weave_sub, "needs_submission")
    return actor


@pytest.fixture
def minor(make_actor, weave: StubWeaveClient) -> Actor:
    """A Ganymede member under 18, ID-verified, with no verified guardian."""
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Milo Minor")
    weave.set_minor(actor.user.weave_sub, True)
    return actor


def _submitted(db_session: Session, submitter: Actor, *, builders=(), budget_cents: int = 10_000) -> Project:
    project = new_project(
        db_session, submitter, title="Rover", write_up="A rover.", budget_requested_cents=budget_cents
    )
    for builder in builders:
        add_to_team(db_session, project, builder)
    return projects.submit(db_session, submitter, project=project, criteria_read=True)


def _approve(db_session: Session, reviewer: Actor, project: Project, weave_client) -> Project:
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave_client,
    )
    db_session.refresh(project)
    return project


def _approved(db_session: Session, submitter: Actor, reviewer: Actor, weave_client, *, builders=()) -> Project:
    return _approve(db_session, reviewer, _submitted(db_session, submitter, builders=builders), weave_client)


def _committed(db_session: Session, project: Project) -> list[dict]:
    db_session.flush()
    rows = db_session.scalars(
        sa.select(QuiltOutbox)
        .where(QuiltOutbox.external_id == str(project.id), QuiltOutbox.type == BUDGET_COMMITTED)
        .order_by(QuiltOutbox.seq)
    )
    return [row.payload for row in rows]


def _actions(db_session: Session, project: Project) -> list[str]:
    """The project's grant audit actions, sorted: a test's writes share one transaction, so one `created_at`."""
    db_session.flush()
    return sorted(
        db_session.scalars(
            sa.select(AuditEvent.action).where(
                AuditEvent.project_id == project.id, AuditEvent.action.like("budget_grant_%")
            )
        )
    )


# -- Held at approval ---------------------------------------------------------------------------------------


def test_a_cleared_team_is_granted_its_budget_at_once(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)

    assert budget.ceiling_cents(db_session, project) == 10_000
    assert grants.pending(db_session, project) is None
    assert [payload["amount_cents"] for payload in _committed(db_session, project)] == [10_000]


def test_an_unverified_team_is_approved_but_its_budget_is_held(
    db_session: Session, unverified: Actor, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave, builders=[minor])

    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 0
    assert _committed(db_session, project) == []
    held = grants.pending(db_session, project)
    assert (held.kind, held.amount_cents, held.actor_id) == (
        BudgetEntryKind.INITIAL_APPROVAL,
        10_000,
        reviewer.user.id,
    )
    [event] = db_session.scalars(
        sa.select(AuditEvent).where(AuditEvent.project_id == project.id, AuditEvent.action == grants.AUDIT_GRANT_HELD)
    )
    # Patchwork IDs only: no names, and nothing that says who is under 18.
    assert event.payload == {
        "amount_cents": 10_000,
        "kind": "initial_approval",
        "waiting_on": [unverified.user.weave_sub, minor.user.weave_sub],
    }


def test_weave_down_at_approval_holds_the_budget(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member)
    # The reviewer's own role check goes to the real stub; only the grant's clearance check sees Weave down.
    projects._apply_approve(db_session, reviewer, project.current_revision, weave_client=_DownWeave())

    assert budget.ceiling_cents(db_session, project) == 0
    held = grants.pending(db_session, project)
    assert held.amount_cents == 10_000
    event = db_session.scalars(
        sa.select(AuditEvent).where(AuditEvent.project_id == project.id, AuditEvent.action == grants.AUDIT_GRANT_HELD)
    ).one()
    assert event.payload["weave_unavailable"] is True


# -- Release ------------------------------------------------------------------------------------------------


def test_the_release_writes_the_entry_once_the_team_is_cleared(
    db_session: Session, unverified: Actor, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave, builders=[minor])

    weave.set_verification_status(unverified.user.weave_sub, "verified")
    assert grants.release_ready(db_session, weave, now=NOW) == 0  # the minor still has no verified guardian
    assert budget.ceiling_cents(db_session, project) == 0

    weave.set_guardian_verified(minor.user.weave_sub, True)
    assert grants.release_ready(db_session, weave, now=NOW) == 1

    db_session.refresh(project)
    assert budget.ceiling_cents(db_session, project) == 10_000
    assert grants.pending(db_session, project) is None
    held = db_session.scalars(sa.select(HeldGrant).where(HeldGrant.project_id == project.id)).one()
    entry = db_session.get(BudgetEntry, held.budget_entry_id)
    assert held.released_at == NOW
    assert (entry.kind, entry.amount_cents, entry.actor_id, entry.revision_id) == (
        BudgetEntryKind.INITIAL_APPROVAL,
        10_000,
        reviewer.user.id,
        project.approved_revision_id,
    )
    [committed] = _committed(db_session, project)
    assert (committed["amount_cents"], committed["actor_sub"]) == (10_000, reviewer.user.weave_sub)
    assert _actions(db_session, project) == sorted([grants.AUDIT_GRANT_HELD, grants.AUDIT_GRANT_RELEASED])

    # Nothing is released twice.
    assert grants.release_ready(db_session, weave, now=NOW) == 0
    assert budget.ceiling_cents(db_session, project) == 10_000


def test_weave_down_at_release_keeps_the_hold(
    db_session: Session, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave)
    weave.set_verification_status(unverified.user.weave_sub, "verified")

    assert grants.release_ready(db_session, _DownWeave(), now=NOW) == 0

    assert grants.pending(db_session, project) is not None
    assert budget.ceiling_cents(db_session, project) == 0


def test_the_release_asks_about_the_team_as_it_is_then(
    db_session: Session, member: Actor, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    # The team as it is at release decides, not as it was at approval.
    project = _submitted(db_session, member, builders=[unverified])
    weave.set_verification_status(unverified.user.weave_sub, "verified")
    weave.set_verification_status(member.user.weave_sub, "pending")
    project = _approve(db_session, reviewer, project, weave)
    weave.set_verification_status(member.user.weave_sub, "verified")
    weave.set_verification_status(unverified.user.weave_sub, "ineligible")

    assert grants.release_ready(db_session, weave, now=NOW) == 0
    assert budget.ceiling_cents(db_session, project) == 0


# -- Cancelled --------------------------------------------------------------------------------------------


def test_withdrawing_cancels_the_hold(
    db_session: Session, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave)

    projects.withdraw(db_session, unverified, project=project)
    weave.set_verification_status(unverified.user.weave_sub, "verified")

    assert grants.pending(db_session, project) is None
    assert grants.release_ready(db_session, weave, now=NOW) == 0
    assert budget.ceiling_cents(db_session, project) == 0
    assert _committed(db_session, project) == []
    assert _actions(db_session, project) == sorted([grants.AUDIT_GRANT_HELD, grants.AUDIT_GRANT_CANCELLED])


def test_completing_cancels_the_hold(
    db_session: Session, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave)
    projects.start_completion(db_session, unverified, project=project)
    projects.submit_completion(db_session, unverified, project=project)

    project = _approve(db_session, reviewer, project, weave)

    assert project.status is ProjectStatus.COMPLETED
    assert grants.pending(db_session, project) is None
    assert budget.ceiling_cents(db_session, project) == 0


# -- Nothing else changes the budget while it's held ----------------------------------------------------------


def test_an_admin_increase_is_held_too(
    db_session: Session, unverified: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    # Weave stops clearing the team after a funded approval: an admin's increase waits for it again.
    project = _approved(db_session, unverified, reviewer, weave)
    weave.set_verification_status(unverified.user.weave_sub, "verified")
    grants.release_ready(db_session, weave, now=NOW)
    weave.set_verification_status(unverified.user.weave_sub, "pending")

    result = projects.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=5_000, reason="More.", weave_client=weave
    )

    assert isinstance(result, HeldGrant)
    assert (result.kind, result.amount_cents) == (BudgetEntryKind.ADMIN_ADJUSTMENT, 5_000)
    assert budget.ceiling_cents(db_session, project) == 10_000

    weave.set_verification_status(unverified.user.weave_sub, "verified")
    assert grants.release_ready(db_session, weave, now=NOW) == 1
    assert budget.ceiling_cents(db_session, project) == 15_000


def test_an_admin_increase_for_a_cleared_team_is_written_at_once(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)

    result = projects.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=5_000, reason="More.", weave_client=weave
    )

    assert isinstance(result, BudgetEntry)
    assert budget.ceiling_cents(db_session, project) == 15_000


@pytest.mark.parametrize("amount_cents", [5_000, -1])
def test_an_admin_cant_adjust_a_held_budget(
    db_session: Session, unverified: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient, amount_cents: int
) -> None:
    project = _approved(db_session, unverified, reviewer, weave)
    if amount_cents < 0:
        # A cut needs a ceiling above zero to cut from: fund the project, then hold an increase.
        weave.set_verification_status(unverified.user.weave_sub, "verified")
        grants.release_ready(db_session, weave, now=NOW)
        projects.admin_adjust_budget(
            db_session, admin, project=project, amount_cents=1_000, reason="More.", weave_client=_DownWeave()
        )

    with pytest.raises(InvalidState, match="budget is held"):
        projects.admin_adjust_budget(
            db_session, admin, project=project, amount_cents=amount_cents, reason="Again.", weave_client=weave
        )


def test_reclaiming_is_refused_while_a_grant_is_held(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    projects.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=1_000, reason="More.", weave_client=_DownWeave()
    )

    with pytest.raises(InvalidState, match="budget is held"):
        projects.reclaim_budget(db_session, admin, project=project, amount_cents=500, reason="Less.")


def test_an_amendment_cant_start_while_a_grant_is_held(
    db_session: Session, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave)

    with pytest.raises(InvalidState, match="budget is held"):
        projects.start_amendment(db_session, unverified, project=project)


def test_an_amendment_that_raises_the_ceiling_is_held(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    draft = projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=25_000)
    projects.submit(db_session, member, project=project, criteria_read=True)
    weave.set_verification_status(member.user.weave_sub, "pending")

    project = _approve(db_session, reviewer, project, weave)

    assert project.approved_revision_id == draft.id
    assert budget.ceiling_cents(db_session, project) == 10_000
    held = grants.pending(db_session, project)
    assert (held.kind, held.amount_cents, held.revision_id) == (BudgetEntryKind.AMENDMENT, 15_000, draft.id)


def test_an_amendment_that_cuts_the_ceiling_isnt_held(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=4_000)
    projects.submit(db_session, member, project=project, criteria_read=True)
    weave.set_verification_status(member.user.weave_sub, "pending")

    project = _approve(db_session, reviewer, project, weave)

    assert budget.ceiling_cents(db_session, project) == 4_000
    assert grants.pending(db_session, project) is None


def test_an_amendment_in_review_cant_be_approved_while_an_admin_increase_is_held(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=25_000)
    projects.submit(db_session, member, project=project, criteria_read=True)
    projects.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=1_000, reason="More.", weave_client=_DownWeave()
    )

    with pytest.raises(InvalidState, match="budget is held"):
        _approve(db_session, reviewer, project, weave)


def test_the_database_allows_one_pending_hold_per_project(
    db_session: Session, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, unverified, reviewer, weave)
    db_session.add(
        HeldGrant(
            project_id=project.id, kind=BudgetEntryKind.ADMIN_ADJUSTMENT, amount_cents=1, actor_id=reviewer.user.id
        )
    )

    with pytest.raises(sa.exc.IntegrityError):
        db_session.flush()
