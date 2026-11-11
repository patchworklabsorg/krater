"""The scheduler's guards before compute goes live: the stop switch, the organization spending caps, the per-project
machine limit, the idle-GPU warning for batch jobs, and the channel notice when a batch job ends."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import (
    AuditEvent,
    ComputeInstanceState,
    ComputeJobState,
    ComputeSettings,
    ProjectComputeSettings,
    SpendSnapshot,
    SpendSource,
)
from krater.services import compute_halt, compute_jobs
from krater.services.errors import InvalidState
from tests.compute.test_boxes import _beat as _box_beat
from tests.compute.test_boxes import _running_box
from tests.compute.test_scheduler import NOW, _events, _instance, _pass, _runner_starts, _submit


def _running_job(db_session, member, project, client, fake, *, minute=0, **overrides):  # noqa: F811
    job = _submit(db_session, member, project, now=NOW + timedelta(minutes=minute), **overrides)
    _pass(db_session, client, NOW + timedelta(minutes=minute))
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=minute + 1))
    _pass(db_session, client, NOW + timedelta(minutes=minute + 1))
    assert job.state is ComputeJobState.RUNNING
    return job, row


def _settings_row(db_session) -> ComputeSettings:
    compute_halt.current(db_session)
    return db_session.get(ComputeSettings, 1)


def _audits(db_session, action):
    return list(db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == action)))


# -- The stop switch -----------------------------------------------------------------------------------------------


def test_stopping_compute_destroys_a_jobs_machine_and_requeues_the_job(
    db_session, member, admin, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)

    compute_halt.pause(db_session, admin, reason="spend looks wrong", now=NOW + timedelta(minutes=2))
    _pass(db_session, client, NOW + timedelta(minutes=3))

    assert row.state is ComputeInstanceState.DESTROYED and row.destroy_reason == "compute stopped"
    assert row.provider_instance_id not in fake.instances
    assert job.state is ComputeJobState.QUEUED
    assert "stopped Krater Compute" in job.state_reason
    assert "halted" in _events(db_session, job)


def test_while_stopped_nothing_is_searched_or_rented(db_session, member, admin, project, client, fake) -> None:  # noqa: F811
    compute_halt.pause(db_session, admin, reason="r", now=NOW)
    job = _submit(db_session, member, project)

    _pass(db_session, client, NOW + timedelta(minutes=1))

    assert job.state is ComputeJobState.QUEUED
    assert job.state_reason == f"Waiting: {compute_halt.PAUSED_REASON}."
    assert fake.search_bodies == [] and fake.instances == {}


def test_turning_compute_back_on_gives_queued_jobs_the_stopped_time_back(
    db_session, member, admin, project, client, fake
) -> None:  # noqa: F811
    job = _submit(db_session, member, project, max_wait_minutes=60)
    compute_halt.pause(db_session, admin, reason="r", now=NOW)
    _pass(db_session, client, NOW)
    _pass(db_session, client, NOW + timedelta(hours=5))
    assert _settings_row(db_session).halted_since == NOW

    compute_halt.resume(db_session, admin, reason="fixed", now=NOW + timedelta(hours=5, minutes=1))
    fake.offers.clear()  # nothing to rent: the job must stay queued, not cancel for the stopped hours
    _pass(db_session, client, NOW + timedelta(hours=5, minutes=2))

    db_session.refresh(job)
    assert job.queued_at == NOW + timedelta(hours=5, minutes=2)
    assert job.state is ComputeJobState.QUEUED
    assert _settings_row(db_session).halted_since is None


def test_a_running_box_saves_its_workspace_and_stops_when_compute_is_stopped(
    db_session, member, admin, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)

    compute_halt.pause(db_session, admin, reason="r", now=NOW + timedelta(minutes=3))
    _pass(db_session, client, NOW + timedelta(minutes=3))
    assert row.stop_requested_at is not None
    assert _box_beat(db_session, row, 4).sync is True
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=4, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert row.state is ComputeInstanceState.STOPPED and job.state is ComputeJobState.STOPPED
    assert row.provider_instance_id in fake.instances  # its disk is kept


def test_a_box_cant_be_resumed_while_compute_is_stopped(
    db_session, member, admin, project, client, fake, weave
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=5))
    _pass(db_session, client, NOW + timedelta(minutes=4))
    assert job.state is ComputeJobState.STOPPED

    compute_halt.pause(db_session, admin, reason="r", now=NOW + timedelta(minutes=5))
    try:
        compute_jobs.resume_box(db_session, member, job=job, now=NOW + timedelta(minutes=6), weave_client=weave)
    except InvalidState as exc:
        assert "stopped Krater Compute" in str(exc)
    else:
        raise AssertionError("resumed while compute was stopped")


def test_a_resume_asked_for_before_the_stop_is_taken_back(
    db_session, member, admin, project, client, fake, weave
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=5))
    _pass(db_session, client, NOW + timedelta(minutes=4))
    compute_jobs.resume_box(db_session, member, job=job, now=NOW + timedelta(minutes=5), weave_client=weave)

    compute_halt.pause(db_session, admin, reason="r", now=NOW + timedelta(minutes=5, seconds=30))
    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert row.start_requested_at is None
    assert fake.instances[row.provider_instance_id]["intended_status"] == "stopped"
    assert "Not resumed" in job.state_reason


# -- Spending caps -------------------------------------------------------------------------------------------------


def _spend(db_session, project, cents, at):
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=cents, source=SpendSource.VAST_ESTIMATE, taken_at=at)
    )
    db_session.flush()


def test_spend_since_counts_each_projects_rise_and_never_a_fall(db_session, project) -> None:  # noqa: F811
    _spend(db_session, project, 100, NOW - timedelta(days=2))
    _spend(db_session, project, 400, NOW - timedelta(hours=1))

    assert compute_halt.spend_since(db_session, NOW - timedelta(days=1)) == {project.id: 300}
    assert compute_halt.spend_since(db_session, None) == {project.id: 400}
    _spend(db_session, project, 50, NOW)  # the reconciliation lowered it below the day's start
    assert compute_halt.spend_since(db_session, NOW - timedelta(days=1)) == {project.id: 0}


def test_the_daily_cap_stops_compute_and_lifts_the_next_day(db_session, member, project, client, fake) -> None:  # noqa: F811
    row = _settings_row(db_session)
    row.org_daily_cap_cents = 500
    _spend(db_session, project, 100, NOW.replace(hour=0) - timedelta(minutes=5))
    _spend(db_session, project, 650, NOW - timedelta(minutes=5))
    job = _submit(db_session, member, project)

    _pass(db_session, client)
    assert job.state is ComputeJobState.QUEUED and "daily spending cap" in job.state_reason
    assert fake.instances == {}
    assert compute_halt.current(db_session).startswith("Krater has reached its daily spending cap")

    _pass(db_session, client, NOW.replace(hour=0) + timedelta(days=1, minutes=1))
    assert job.state is ComputeJobState.STARTING
    assert _settings_row(db_session).halted_since is None


def test_a_cap_of_zero_is_off(db_session, member, project, client) -> None:  # noqa: F811
    _spend(db_session, project, 5_000, NOW - timedelta(minutes=5))
    job = _submit(db_session, member, project)

    _pass(db_session, client)

    assert job.state is ComputeJobState.STARTING


# -- Machines at once ----------------------------------------------------------------------------------------------


def test_a_project_at_its_machine_limit_waits_without_searching(db_session, member, project, client, fake) -> None:  # noqa: F811
    db_session.add(ProjectComputeSettings(project_id=project.id, max_machines=1))
    db_session.flush()
    first, _ = _running_job(db_session, member, project, client, fake)
    second = _submit(db_session, member, project, now=NOW + timedelta(minutes=2))
    searches = len(fake.search_bodies)

    _pass(db_session, client, NOW + timedelta(minutes=3))

    assert second.state is ComputeJobState.QUEUED
    assert "1 machines at once, its limit" in second.state_reason
    assert len(fake.search_bodies) == searches

    compute_jobs.cancel_job(db_session, member, job=first, now=NOW + timedelta(minutes=4))
    _pass(db_session, client, NOW + timedelta(minutes=5))  # destroys the first machine
    _pass(db_session, client, NOW + timedelta(minutes=6))
    assert second.state is ComputeJobState.STARTING


def test_by_default_four_machines_run_at_once(db_session, member, project, client, fake) -> None:  # noqa: F811
    jobs = [_submit(db_session, member, project, name=f"sweep-{n}", target_microusd=100_000) for n in range(5)]
    for n in range(5):
        fake.offers[9000 + n] = dict(fake.offers[1001], id=9000 + n, machine_id=9000 + n)

    _pass(db_session, client)

    states = [job.state for job in jobs]
    assert states.count(ComputeJobState.STARTING) == 4
    assert "its limit" in jobs[-1].state_reason


# -- Idle GPU warning ----------------------------------------------------------------------------------------------


def _beat(db_session, row, minute, util):
    runner_api.heartbeat(db_session, row, {"gpu_util": util}, now=NOW + timedelta(minutes=minute))


def test_a_batch_job_with_an_idle_gpu_gets_one_warning(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)

    for minute in range(2, 40):
        _beat(db_session, row, minute, 1)
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    warnings = _audits(db_session, compute_jobs.AUDIT_JOB_IDLE)
    assert len(warnings) == 1
    assert warnings[0].payload["job_name"] == "train" and warnings[0].payload["minutes"] == 30
    assert job.state is ComputeJobState.RUNNING  # a warning, not a stop
    assert "idle_warning" in _events(db_session, job)


def test_a_busy_gpu_gets_no_warning(db_session, member, project, client, fake) -> None:  # noqa: F811
    _, row = _running_job(db_session, member, project, client, fake)

    for minute in range(2, 40):
        _beat(db_session, row, minute, 0 if minute % 20 else 85)  # short idle stretches only
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert _audits(db_session, compute_jobs.AUDIT_JOB_IDLE) == []


def test_the_idle_warning_can_be_turned_off(db_session, member, project, client, fake) -> None:  # noqa: F811
    db_session.add(ProjectComputeSettings(project_id=project.id, job_idle_warn_minutes=0))
    db_session.flush()
    _, row = _running_job(db_session, member, project, client, fake)

    for minute in range(2, 40):
        _beat(db_session, row, minute, 0)
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert _audits(db_session, compute_jobs.AUDIT_JOB_IDLE) == []


# -- Job-ended notices ---------------------------------------------------------------------------------------------


def test_a_finished_job_gets_one_notice(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    runner_api.report_exit(db_session, row, 0, now=NOW + timedelta(minutes=5))

    _pass(db_session, client, NOW + timedelta(minutes=6))
    _pass(db_session, client, NOW + timedelta(minutes=7))

    notices = _audits(db_session, compute_jobs.AUDIT_JOB_ENDED)
    assert len(notices) == 1
    assert notices[0].payload["state"] == "succeeded" and notices[0].payload["job_id"] == str(job.id)
    assert notices[0].project_id == project.id


def test_a_members_own_cancel_gets_no_notice_but_an_admins_does(
    db_session, member, admin, project, client, fake
) -> None:  # noqa: F811
    mine = _submit(db_session, member, project, name="mine")
    theirs = _submit(db_session, member, project, name="theirs")
    compute_jobs.cancel_job(db_session, member, job=mine, now=NOW)
    compute_jobs.cancel_job(db_session, admin, job=theirs, now=NOW)

    _pass(db_session, client, NOW + timedelta(minutes=1))

    notices = _audits(db_session, compute_jobs.AUDIT_JOB_ENDED)
    assert [n.payload["job_name"] for n in notices] == ["theirs"]
    assert mine.end_notified_at is not None


def test_a_job_that_ended_long_before_the_pass_gets_no_notice(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    job.state, job.ended_at, job.state_reason = ComputeJobState.FAILED, NOW - timedelta(hours=3), "Stopped."

    _pass(db_session, client)

    assert _audits(db_session, compute_jobs.AUDIT_JOB_ENDED) == []
    assert job.end_notified_at == NOW


def test_settings_are_read_back_for_the_runner(db_session) -> None:
    # The new policy fields reach the effective policy every compute loop reads.
    from krater.services import compute_settings

    policy = compute_settings.global_policy(db_session)
    assert (policy.max_machines, policy.job_idle_warn_minutes) == (4, 30)
    assert get_settings().compute_store_capacity_gb == 0
