"""The `stalled_jobs_sweep` periodic task: jobs whose worker died end as failed, which frees their locks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import procrastinate

from krater.worker.app import (
    STALLED_SWEEP_LOCK,
    STALLED_WORKER_SECONDS,
    app,
    fail_stalled_jobs,
    stalled_jobs_sweep,
)

Status = procrastinate.jobs.Status


class FakeJobManager:
    """The two `JobManager` calls the sweep makes. `gone`: ids that finish on their own before the sweep ends them."""

    def __init__(self, stalled: list[tuple[int, str]], gone: tuple[int, ...] = ()) -> None:
        self.stalled = [SimpleNamespace(id=job_id, task_name=name) for job_id, name in stalled]
        self.gone = set(gone)
        self.asked_with: float | None = None
        self.finished: list[tuple[int, Status, bool]] = []

    async def get_stalled_jobs(self, *, seconds_since_heartbeat: float) -> list[SimpleNamespace]:
        self.asked_with = seconds_since_heartbeat
        return self.stalled

    async def finish_job_by_id_async(self, job_id: int, status: Status, delete_job: bool) -> None:
        if job_id in self.gone:
            raise procrastinate.exceptions.ConnectorException("Job was not found or not in doing or todo status")
        self.finished.append((job_id, status, delete_job))


def _sweep(manager: FakeJobManager) -> None:
    asyncio.run(stalled_jobs_sweep.func(SimpleNamespace(app=SimpleNamespace(job_manager=manager)), timestamp=1))


def test_the_sweep_runs_every_minute_and_never_queues_twice() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("stalled_jobs_sweep", "")]

    assert periodic_task.cron == "* * * * *"
    assert stalled_jobs_sweep.queueing_lock == STALLED_SWEEP_LOCK
    # It mustn't wait behind the locks it exists to free.
    assert stalled_jobs_sweep.lock is None


def test_orphaned_jobs_end_as_failed_and_are_kept() -> None:
    manager = FakeJobManager([(48632, "compute_schedule"), (7, "slack_invite_team")])

    ended = asyncio.run(fail_stalled_jobs(manager))

    assert manager.asked_with == STALLED_WORKER_SECONDS
    assert manager.finished == [(48632, Status.FAILED, False), (7, Status.FAILED, False)]
    assert ended == ["compute_schedule #48632", "slack_invite_team #7"]


def test_a_job_that_finished_meanwhile_is_skipped() -> None:
    manager = FakeJobManager([(1, "compute_reconcile"), (2, "compute_schedule")], gone=(1,))

    assert asyncio.run(fail_stalled_jobs(manager)) == ["compute_schedule #2"]


def test_the_sweep_opens_the_admin_alert_only_when_it_ended_something() -> None:
    with patch("krater.worker.app._report_failure") as report:
        _sweep(FakeJobManager([]))
        report.assert_not_called()

        _sweep(FakeJobManager([(48632, "compute_schedule")]))

    report.assert_called_once()
    key, message = report.call_args.args
    assert key == "stalled_jobs" and "compute_schedule #48632" in message


def test_a_failing_sweep_is_logged_not_raised() -> None:
    class Broken(FakeJobManager):
        async def get_stalled_jobs(self, *, seconds_since_heartbeat: float) -> list[SimpleNamespace]:
            raise procrastinate.exceptions.ConnectorException("database is down")

    with patch("krater.worker.app._report_failure") as report:
        _sweep(Broken([]))

    report.assert_not_called()
