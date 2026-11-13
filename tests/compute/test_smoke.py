"""Smoke tests (`krater.services.smoke`): the runner stops the command after its minutes and the job passes; the
scheduler's backstop, the budget hold and the estimate use the short cap; the rules on what can be one."""

from __future__ import annotations

from datetime import timedelta

import pytest

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import ComputeInstanceState, ComputeJobKind, ComputeJobState
from krater.services import job_costs, smoke
from krater.services.compute_jobs import JobSpec
from krater.services.errors import ValidationFailed
from tests.compute.test_scheduler import NOW, _pass, _running_job, _submit, _token


def _start(db_session, fake, row):
    return runner_api.start(
        db_session, runner_api.authenticate(db_session, _token(fake, row)), now=NOW, settings=get_settings()
    )


def test_the_runner_is_told_the_smoke_tests_length(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, smoke_minutes=5)

    assert job.smoke_minutes == 5 and job.max_runtime_minutes == 480  # the full run's max runtime is kept
    assert _start(db_session, fake, row).smoke_seconds == 300


def test_an_ordinary_job_has_no_smoke_test(db_session, member, project, client, fake) -> None:  # noqa: F811
    _job, row = _running_job(db_session, member, project, client, fake)
    assert _start(db_session, fake, row).smoke_seconds == 0


def test_a_smoke_test_the_runner_stopped_passes(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, smoke_minutes=5)

    runner_api.report_exit(db_session, row, 143, now=NOW + timedelta(minutes=11), smoke_stopped=True)

    assert job.state is ComputeJobState.SUCCEEDED and job.exit_code is None
    assert job.state_reason == "Smoke test passed: the command ran 5 minutes without failing."
    assert job.hold_cents == 0


def test_a_smoke_test_whose_command_finishes_first_passes(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, smoke_minutes=5)
    runner_api.report_exit(db_session, row, 0, now=NOW + timedelta(minutes=7))
    assert job.state is ComputeJobState.SUCCEEDED and job.exit_code == 0
    assert job.state_reason == "Smoke test passed: the command finished."


def test_a_smoke_test_that_fails_says_so(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, smoke_minutes=5)
    runner_api.report_exit(db_session, row, 2, now=NOW + timedelta(minutes=7))
    assert job.state is ComputeJobState.FAILED and job.exit_code == 2
    assert job.state_reason == "Smoke test failed: the command exited with code 2."


def test_only_a_smoke_job_can_say_its_stop_was_a_pass(db_session, member, project, client, fake) -> None:  # noqa: F811
    # The runner runs the member's code as root: an ordinary job's runner claiming a smoke stop changes nothing.
    job, row = _running_job(db_session, member, project, client, fake)
    runner_api.report_exit(db_session, row, 143, now=NOW + timedelta(minutes=7), smoke_stopped=True)
    assert job.state is ComputeJobState.FAILED and job.exit_code == 143


def test_the_scheduler_ends_a_smoke_test_the_runner_never_stopped(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, smoke_minutes=5)

    _pass(db_session, client, NOW + timedelta(minutes=17))  # 12 minutes running: inside the 15-minute backstop
    assert job.state is ComputeJobState.RUNNING

    _pass(db_session, client, NOW + timedelta(minutes=21))  # 16 minutes running
    assert job.state is ComputeJobState.FAILED
    assert job.state_reason.startswith("Smoke test: the machine was stopped after 15 minutes running")
    assert row.state is ComputeInstanceState.DESTROYED and fake.instances == {}


def test_the_budget_hold_is_for_the_smoke_test_not_the_full_run(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, smoke_minutes=5)
    _pass(db_session, client)

    # The cheapest RTX 3060 is about $0.064/h: 8 hours would hold 52 cents, the 15-minute backstop a cent or two.
    assert job.state is ComputeJobState.STARTING
    assert 0 < job.hold_cents <= 3


def test_the_estimate_prices_the_smoke_test(db_session) -> None:
    spec = JobSpec(name="t", image="x", command="go", gpu_names=("RTX 3060",), target_microusd=1, max_microusd=600_000)
    full = job_costs.estimate(db_session, spec, transfer_cap_microusd_per_gb=0, now=NOW)
    short = job_costs.estimate(
        db_session, JobSpec(**{**spec.__dict__, "smoke_minutes": 5}), transfer_cap_microusd_per_gb=0, now=NOW
    )
    assert full.up_to_cents == 480  # $0.60/h for 8 hours
    assert short.up_to_cents == 15  # for the 15-minute backstop


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kind": ComputeJobKind.BOX, "command": None}, "A smoke test is for jobs"),
        ({"smoke_minutes": 31}, "Choose a smoke test of 1 to 30 minutes."),
        ({"smoke_minutes": 0}, "Choose a smoke test of 1 to 30 minutes."),
        ({"smoke_minutes": 20, "max_runtime_minutes": 20}, "The smoke test must be shorter than the max runtime."),
    ],
)
def test_what_cant_be_a_smoke_test(db_session, member, project, overrides, message) -> None:  # noqa: F811
    overrides = {"smoke_minutes": 5, **overrides}
    with pytest.raises(ValidationFailed) as refused:
        _submit(db_session, member, project, **overrides)
    assert refused.value.errors["smoke_minutes"].startswith(message)


def test_the_runtime_cap_is_the_shorter_of_the_two() -> None:
    def cap(max_runtime, minutes):
        return smoke.runtime_cap_minutes(
            JobSpec(
                name="t",
                image="x",
                target_microusd=1,
                max_microusd=1,
                max_runtime_minutes=max_runtime,
                smoke_minutes=minutes,
            )
        )

    assert (cap(480, None), cap(480, 5), cap(12, 5)) == (480, 15, 12)
