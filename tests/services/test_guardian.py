"""The guardian gate (`krater.services.guardian`): no budget grant while a team member is a minor without a
verified guardian in Weave (docs/SPEC.md "Guardian verification")."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.orm import Session

from krater.models import BudgetEntry, ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import budget, guardian, projects
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import GuardianCheckUnavailable, GuardianVerificationRequired
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.factories import new_project
from tests.services.conftest import add_to_team


class _DownWeave:
    """A Weave that can't be reached."""

    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


class _SpyWeave:
    """Records the `fresh` flag of every directory lookup, then asks the real stub."""

    def __init__(self, inner: StubWeaveClient) -> None:
        self.inner = inner
        self.fresh_flags: list[bool] = []

    def get_user(self, sub: str, *, fresh: bool = False):
        self.fresh_flags.append(fresh)
        return self.inner.get_user(sub, fresh=fresh)


@pytest.fixture
def guardian_logs(caplog, monkeypatch):
    """Captures the gate's warnings. Alembic's `fileConfig` (run by the `engine` fixture) disables loggers that
    already exist, so turn this one back on."""
    monkeypatch.setattr(guardian.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger="krater.services.guardian")
    return caplog


@pytest.fixture
def minor(make_actor, weave: StubWeaveClient) -> Actor:
    """A Ganymede member Weave lists as under 18, with no verified guardian."""
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Milo Minor")
    weave.set_minor(actor.user.weave_sub, True)
    return actor


def _submitted(db_session: Session, submitter: Actor, *, budget_cents: int = 10_000, builders=()):
    project = new_project(
        db_session, submitter, title="Rover", write_up="A rover.", budget_requested_cents=budget_cents
    )
    for builder in builders:
        add_to_team(db_session, project, builder)
    return projects.submit(db_session, submitter, project=project)


def _approve(db_session: Session, reviewer: Actor, project, weave_client) -> None:
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave_client,
    )


def _ledger_count(db_session: Session, project) -> int:
    return db_session.query(BudgetEntry).filter(BudgetEntry.project_id == project.id).count()


# -- Proposal approval ---------------------------------------------------------------------------------


def test_a_minor_without_a_verified_guardian_blocks_the_proposal_approval(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, minor)

    with pytest.raises(GuardianVerificationRequired) as exc_info:
        _approve(db_session, reviewer, project, weave)

    assert exc_info.value.members == [(f"Milo Minor ({minor.user.weave_sub})", guardian.REASON_NO_VERIFIED_GUARDIAN)]
    assert "verified guardian in Weave" in str(exc_info.value)
    assert project.status is ProjectStatus.PENDING_REVIEW
    assert project.current_revision.outcome is RevisionOutcome.PENDING
    assert _ledger_count(db_session, project) == 0


def test_a_minor_with_a_verified_guardian_can_be_approved(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    weave.set_guardian_verified(minor.user.weave_sub, True)
    project = _submitted(db_session, minor)

    _approve(db_session, reviewer, project, weave)

    assert project.status is ProjectStatus.APPROVED
    assert budget.ceiling_cents(db_session, project) == 10_000


def test_a_false_guardian_verified_still_blocks(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    weave.set_guardian_verified(minor.user.weave_sub, False)
    project = _submitted(db_session, minor)

    with pytest.raises(GuardianVerificationRequired):
        _approve(db_session, reviewer, project, weave)


def test_an_adult_is_not_blocked(db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient) -> None:
    weave.set_minor(member.user.weave_sub, False)
    project = _submitted(db_session, member)

    _approve(db_session, reviewer, project, weave)

    assert project.status is ProjectStatus.APPROVED


def test_a_missing_minor_field_counts_as_not_known_to_be_a_minor_and_is_logged(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient, guardian_logs
) -> None:
    project = _submitted(db_session, member)

    _approve(db_session, reviewer, project, weave)

    assert project.status is ProjectStatus.APPROVED
    assert member.user.weave_sub in guardian_logs.text
    assert "no minor field" in guardian_logs.text


def test_a_team_member_weave_does_not_return_blocks_the_grant(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    # Weave's directory answers 404 for anyone Krater may not serve, such as a minor whose consent was revoked.
    project = _submitted(db_session, member)
    weave.remove_user(member.user.weave_sub)

    with pytest.raises(GuardianVerificationRequired) as exc_info:
        _approve(db_session, reviewer, project, weave)

    assert exc_info.value.members == [(member.user.weave_sub, guardian.REASON_NOT_IN_WEAVE)]
    assert "not available in Weave (no access to Krater)" in str(exc_info.value)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_teammate_weave_does_not_return_blocks_the_grant(
    db_session: Session, member: Actor, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    # A minor whose guardian consent was revoked: Weave hides them, so the 404 must not let the grant through.
    project = _submitted(db_session, member, builders=[minor])
    weave.remove_user(minor.user.weave_sub)

    with pytest.raises(GuardianVerificationRequired) as exc_info:
        _approve(db_session, reviewer, project, weave)

    assert exc_info.value.members == [(minor.user.weave_sub, guardian.REASON_NOT_IN_WEAVE)]


def test_both_reasons_are_listed_together(
    db_session: Session, minor: Actor, make_actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    hidden = make_actor(groups=frozenset({GROUP_MEMBER}))
    project = _submitted(db_session, minor, builders=[hidden])
    weave.remove_user(hidden.user.weave_sub)

    pending = guardian.members_needing_verification(db_session, weave, project)

    assert [(member.user.id, member.reason) for member in pending] == [
        (minor.user.id, guardian.REASON_NO_VERIFIED_GUARDIAN),
        (hidden.user.id, guardian.REASON_NOT_IN_WEAVE),
    ]


def test_a_minor_teammate_blocks_the_approval(
    db_session: Session, member: Actor, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, member, builders=[minor])

    with pytest.raises(GuardianVerificationRequired) as exc_info:
        _approve(db_session, reviewer, project, weave)

    assert exc_info.value.members == [(f"Milo Minor ({minor.user.weave_sub})", guardian.REASON_NO_VERIFIED_GUARDIAN)]


def test_the_gate_asks_weave_fresh(db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient) -> None:
    project = _submitted(db_session, member)
    spy = _SpyWeave(weave)

    _approve(db_session, reviewer, project, spy)

    assert spy.fresh_flags == [True]


def test_the_grant_fails_closed_when_weave_is_down(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted(db_session, member)

    with pytest.raises(GuardianCheckUnavailable):
        _approve(db_session, reviewer, project, _DownWeave())

    assert project.status is ProjectStatus.PENDING_REVIEW


def test_a_rejection_does_not_need_the_gate(db_session: Session, minor: Actor, reviewer: Actor) -> None:
    project = _submitted(db_session, minor)

    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="Too vague.",
        source=ReviewSource.WEB,
        weave_client=_DownWeave(),
    )

    assert project.status is ProjectStatus.CHANGES_REQUESTED


# -- Admin overrides: no bypass ------------------------------------------------------------------------


def test_an_admin_approval_cannot_bypass_the_gate(
    db_session: Session, minor: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, minor)

    with pytest.raises(GuardianVerificationRequired):
        projects.admin_decide(
            db_session,
            admin,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            reason="Urgent.",
            weave_client=weave,
        )

    assert project.status is ProjectStatus.PENDING_REVIEW


def _approved(db_session: Session, submitter: Actor, reviewer: Actor, weave: StubWeaveClient):
    """An approved project. The submitter is cleared for the approval, then made a minor without a
    verified guardian, as when Weave learns a birth date later."""
    project = _submitted(db_session, submitter)
    _approve(db_session, reviewer, project, weave)
    weave.set_minor(submitter.user.weave_sub, True)
    return project


def test_an_admin_budget_increase_is_blocked_but_a_cut_is_not(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)

    with pytest.raises(GuardianVerificationRequired):
        projects.admin_adjust_budget(
            db_session, admin, project=project, amount_cents=5_000, reason="More.", weave_client=weave
        )
    projects.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=-2_000, reason="Less.", weave_client=weave
    )

    assert budget.ceiling_cents(db_session, project) == 8_000


# -- Amendments and completion -------------------------------------------------------------------------


def _amendment(db_session: Session, project, submitter: Actor, *, budget_cents: int):
    projects.start_amendment(db_session, submitter, project=project)
    projects.update_draft(db_session, submitter, project=project, budget_requested_cents=budget_cents)
    projects.submit(db_session, submitter, project=project)
    return project.current_revision


def test_an_amendment_that_raises_the_ceiling_is_blocked(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _amendment(db_session, project, member, budget_cents=20_000)

    with pytest.raises(GuardianVerificationRequired):
        _approve(db_session, reviewer, project, weave)


def test_an_amendment_that_lowers_the_ceiling_is_not_blocked(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    revision = _amendment(db_session, project, member, budget_cents=4_000)

    _approve(db_session, reviewer, project, weave)

    assert revision.outcome is RevisionOutcome.APPROVED
    assert budget.ceiling_cents(db_session, project) == 4_000


def test_a_completion_approval_is_not_blocked(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    projects.start_completion(db_session, member, project=project)
    projects.submit_completion(db_session, member, project=project)

    _approve(db_session, reviewer, project, weave)

    assert project.status is ProjectStatus.COMPLETED


# -- Page display --------------------------------------------------------------------------------------


def test_pending_for_display_lists_the_members_and_uses_the_cache(
    db_session: Session, minor: Actor, weave: StubWeaveClient
) -> None:
    project = _submitted(db_session, minor)
    spy = _SpyWeave(weave)

    pending = guardian.pending_for_display(db_session, spy, project)

    assert [member.user.id for member in pending] == [minor.user.id]
    assert spy.fresh_flags == [False]


def test_pending_for_display_is_none_when_weave_is_down(db_session: Session, minor: Actor) -> None:
    project = _submitted(db_session, minor)

    assert guardian.pending_for_display(db_session, _DownWeave(), project) is None
