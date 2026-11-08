"""Who Weave clears (`krater.services.clearance`): a project's compute waits until Weave clears every team member,
ID verification and a verified guardian for a member under 18 (docs/SPEC.md "Verification before funding"). Its
budget waits too: tests/services/test_grants.py."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.orm import Session

from krater.models import ComputeJobState, ReviewDecision, ReviewSource
from krater.services import clearance, compute_jobs, grants, guardian, projects
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.compute_jobs import JobSpec
from krater.services.errors import ClearanceCheckUnavailable, VerificationRequired
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.compute.test_scheduler import NOW, RUNNER_SHA, SETTINGS
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
def clearance_logs(caplog, monkeypatch):
    """Captures the check's warnings. Alembic's `fileConfig` (run by the `engine` fixture) disables loggers that
    already exist, so turn this one back on."""
    monkeypatch.setattr(clearance.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger="krater.services.clearance")
    return caplog


@pytest.fixture
def minor(make_actor, weave: StubWeaveClient) -> Actor:
    """A Ganymede member Weave lists as under 18, ID-verified, with no verified guardian."""
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Milo Minor")
    weave.set_minor(actor.user.weave_sub, True)
    return actor


@pytest.fixture
def unverified(make_actor, weave: StubWeaveClient) -> Actor:
    """An adult Ganymede member who hasn't done ID verification in Weave."""
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Una Unverified")
    weave.set_minor(actor.user.weave_sub, False)
    weave.set_verification_status(actor.user.weave_sub, "needs_submission")
    return actor


def _approved(db_session: Session, submitter: Actor, reviewer: Actor, weave, *, builders=()):
    project = new_project(db_session, submitter, title="Rover", write_up="A rover.", budget_requested_cents=10_000)
    for builder in builders:
        add_to_team(db_session, project, builder)
    projects.submit(db_session, submitter, project=project, criteria_read=True)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    return project


def _submit_job(db_session: Session, actor: Actor, project, weave_client):
    spec = JobSpec(
        name="train",
        image="ghcr.io/alice/train:v3",
        command="python train.py",
        gpu_names=("RTX 3060",),
        target_microusd=70_000,
        max_microusd=100_000,
    )
    return compute_jobs.submit_job(
        db_session, actor, project=project, spec=spec, cap_cents=500, now=NOW, weave_client=weave_client
    )


# -- Compute waits ---------------------------------------------------------------------------------------


def test_a_minor_without_a_verified_guardian_holds_the_compute(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, minor, reviewer, weave)

    with pytest.raises(VerificationRequired) as exc_info:
        _submit_job(db_session, minor, project, weave)

    assert exc_info.value.members == [(f"Milo Minor ({minor.user.weave_sub})", guardian.REASON_NO_VERIFIED_GUARDIAN)]
    assert "compute is held until every team member is cleared in Weave" in str(exc_info.value)
    weave.set_guardian_verified(minor.user.weave_sub, True)
    grants.release_ready(db_session, weave, now=NOW)  # its budget was held too (tests/services/test_grants.py)
    assert _submit_job(db_session, minor, project, weave).state is ComputeJobState.QUEUED


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("needs_submission", "hasn't done ID verification in Weave"),
        ("pending", "ID verification in Weave is still in review"),
        ("ineligible", "didn't pass ID verification in Weave"),
    ],
)
def test_a_member_without_id_verification_holds_the_compute(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient, status: str, reason: str
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    weave.set_verification_status(member.user.weave_sub, status)

    with pytest.raises(VerificationRequired) as exc_info:
        _submit_job(db_session, member, project, weave)

    assert exc_info.value.members[0][1] == reason
    weave.set_verification_status(member.user.weave_sub, "verified")
    assert _submit_job(db_session, member, project, weave).state is ComputeJobState.QUEUED


def test_a_missing_verification_field_holds_the_compute_and_is_logged(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient, clearance_logs
) -> None:
    # An older Weave that doesn't send the field holds compute rather than letting it through.
    project = _approved(db_session, member, reviewer, weave)
    weave.set_verification_status(member.user.weave_sub, None)

    with pytest.raises(VerificationRequired) as exc_info:
        _submit_job(db_session, member, project, weave)

    assert exc_info.value.members[0][1] == clearance.REASON_ID_UNKNOWN
    assert "no verification_status" in clearance_logs.text


def test_a_teammate_holds_the_whole_projects_compute(
    db_session: Session, member: Actor, unverified: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave, builders=[unverified])

    with pytest.raises(VerificationRequired) as exc_info:
        _submit_job(db_session, member, project, weave)

    assert exc_info.value.members == [
        (f"Una Unverified ({unverified.user.weave_sub})", "hasn't done ID verification in Weave")
    ]


def test_a_minor_without_either_check_gets_both_reasons(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, minor, reviewer, weave)
    weave.set_verification_status(minor.user.weave_sub, "pending")

    pending = clearance.members_not_cleared(db_session, weave, project)

    assert [member.reason for member in pending] == [
        f"{guardian.REASON_NO_VERIFIED_GUARDIAN}; ID verification in Weave is still in review"
    ]


def test_a_team_member_weave_does_not_return_holds_the_compute(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    weave.remove_user(member.user.weave_sub)

    pending = clearance.members_not_cleared(db_session, weave, project)

    assert [(m.user.id, m.reason) for m in pending] == [(member.user.id, guardian.REASON_NOT_IN_WEAVE)]


def test_the_check_asks_weave_fresh(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)
    spy = _SpyWeave(weave)

    _submit_job(db_session, member, project, spy)

    assert spy.fresh_flags == [True]


def test_the_check_fails_closed_when_weave_is_down(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, member, reviewer, weave)

    with pytest.raises(ClearanceCheckUnavailable):
        _submit_job(db_session, member, project, _DownWeave())


# -- The scheduler and boxes -------------------------------------------------------------------------------


def test_the_scheduler_doesnt_rent_for_a_team_that_lost_its_verification(
    db_session: Session, member: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    from krater.compute import scheduler
    from krater.vast import FakeVast

    fake = FakeVast()
    fake.add_offer(1001, 1001, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    project = _approved(db_session, member, reviewer, weave)
    job = _submit_job(db_session, member, project, weave)
    weave.set_verification_status(member.user.weave_sub, "ineligible")

    result = scheduler.run_pass(
        db_session, fake.client(), SETTINGS, now=NOW, runner_sha256=RUNNER_SHA, weave_client=weave
    )

    assert result.rented == [] and fake.instances == {}
    assert job.state is ComputeJobState.QUEUED
    assert job.state_reason.startswith("Waiting until every team member is verified in Weave:")
    assert "didn't pass ID verification in Weave" in job.state_reason

    scheduler.run_pass(
        db_session, fake.client(), SETTINGS, now=NOW, runner_sha256=RUNNER_SHA, weave_client=_DownWeave()
    )
    assert job.state_reason == "Waiting: Krater couldn't reach Weave to check the team is verified."

    weave.set_verification_status(member.user.weave_sub, "verified")
    result = scheduler.run_pass(
        db_session, fake.client(), SETTINGS, now=NOW, runner_sha256=RUNNER_SHA, weave_client=weave
    )
    assert len(fake.instances) == 1


# -- The project page ------------------------------------------------------------------------------------


def test_pending_for_display_lists_the_members_and_uses_the_cache(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, minor, reviewer, weave)
    spy = _SpyWeave(weave)

    pending = clearance.pending_for_display(db_session, spy, project)

    assert [member.user.id for member in pending] == [minor.user.id]
    assert spy.fresh_flags == [False]


def test_pending_for_display_is_none_when_weave_is_down(
    db_session: Session, minor: Actor, reviewer: Actor, weave: StubWeaveClient
) -> None:
    project = _approved(db_session, minor, reviewer, weave)

    assert clearance.pending_for_display(db_session, _DownWeave(), project) is None
