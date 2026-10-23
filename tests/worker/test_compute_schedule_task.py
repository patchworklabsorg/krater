"""The `compute_schedule` periodic task: every minute, under a lock, and only with a provider in KRATER_COMPUTE_MODE."""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from krater.compute.scheduler import PassResult
from krater.config import get_settings
from krater.worker.app import COMPUTE_SCHEDULER_LOCK, app, compute_schedule


def test_compute_schedule_runs_every_minute_under_a_lock() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("compute_schedule", "")]

    assert periodic_task.cron == "* * * * *"
    assert compute_schedule.lock == COMPUTE_SCHEDULER_LOCK
    assert compute_schedule.queueing_lock == COMPUTE_SCHEDULER_LOCK


def test_compute_schedule_does_nothing_with_compute_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    with (
        patch("krater.vast.get_vast_client") as mock_client,
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
    ):
        compute_schedule.func(timestamp=1_791_700_000)

        mock_client.assert_not_called()
        mock_scheduler.run_pass.assert_not_called()


def test_compute_schedule_does_nothing_without_a_provider_client(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    with (
        patch("krater.providers.get_provider", return_value=None),
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
    ):
        compute_schedule.func(timestamp=1_791_700_000)

        mock_scheduler.run_pass.assert_not_called()


def test_compute_schedule_runs_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    with (
        patch("krater.providers.get_provider") as mock_client,
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value
        mock_scheduler.run_pass.return_value = PassResult()
        compute_schedule.func(timestamp=1_791_700_000)

        args, _ = mock_scheduler.run_pass.call_args
        assert args[0] is session and args[1] is mock_client.return_value
        session.close.assert_called_once()


def test_a_pass_with_provider_errors_counts_towards_the_admin_alert(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    with (
        patch("krater.providers.get_provider"),
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
        patch("krater.worker.app.get_sessionmaker"),
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        mock_scheduler.run_pass.return_value = PassResult(errors=2)
        compute_schedule.func(timestamp=1_791_700_000)

    assert mock_report.call_args.args[0] == "compute_schedule"


def test_compute_reconcile_runs_hourly_under_the_scheduler_lock(monkeypatch) -> None:
    from krater.worker.app import compute_reconcile

    periodic_task = app.periodic_registry.periodic_tasks[("compute_reconcile", "")]
    assert periodic_task.cron == "7 * * * *"
    assert compute_reconcile.lock == COMPUTE_SCHEDULER_LOCK

    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    with patch("krater.worker.app.compute_reconciler") as mock_reconciler:
        compute_reconcile.func(timestamp=1_791_700_000)
        mock_reconciler.run.assert_not_called()


def test_compute_storage_cleanup_is_hourly_and_only_with_vast(monkeypatch) -> None:
    from krater.worker.app import compute_storage_cleanup

    assert app.periodic_registry.periodic_tasks[("compute_storage_cleanup", "")].cron == "37 * * * *"
    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    with patch("krater.worker.app.compute_storage") as mock_cleanup:
        compute_storage_cleanup.func(timestamp=1_791_700_000)
        mock_cleanup.abort_abandoned_uploads.assert_not_called()


def test_compute_reconcile_skips_a_provider_that_reports_no_charges(monkeypatch) -> None:
    from krater import providers
    from krater.worker.app import compute_reconcile

    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    vast = providers.registered()["vast"]
    no_charges = dataclasses.replace(vast, capabilities=dataclasses.replace(vast.capabilities, charges=False))
    with (
        patch.dict(providers._REGISTRY, {"vast": no_charges}),
        patch("krater.worker.app.compute_reconciler") as mock_reconciler,
    ):
        compute_reconcile.func(timestamp=1_791_700_000)
        mock_reconciler.run.assert_not_called()
