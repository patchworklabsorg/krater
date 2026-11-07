"""The `grant_release` periodic task: every five minutes, under a lock, whatever KRATER_COMPUTE_MODE says."""

from __future__ import annotations

from unittest.mock import patch

from krater.config import get_settings
from krater.worker.app import GRANT_RELEASE_LOCK, app, grant_release


def test_grant_release_runs_every_five_minutes_under_a_lock() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("grant_release", "")]

    assert periodic_task.cron == "*/5 * * * *"
    assert grant_release.lock == GRANT_RELEASE_LOCK
    assert grant_release.queueing_lock == GRANT_RELEASE_LOCK


def test_grant_release_runs_with_compute_off_and_kicks_quilt(monkeypatch) -> None:
    # Production runs with compute off; a held grant is still released, and Quilt hears about it.
    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    with (
        patch("krater.worker.app.grants") as mock_grants,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
        patch("krater.worker.app.get_weave_client") as mock_weave,
        patch("krater.worker.app.kick_quilt_delivery") as mock_kick,
    ):
        session = mock_sessionmaker.return_value.return_value
        mock_grants.release_ready.return_value = 1
        grant_release.func(timestamp=1_791_700_000)

        args, kwargs = mock_grants.release_ready.call_args
        assert args == (session, mock_weave.return_value) and "now" in kwargs
        mock_kick.assert_called_once()
        session.close.assert_called_once()


def test_nothing_released_doesnt_kick_quilt() -> None:
    with (
        patch("krater.worker.app.grants") as mock_grants,
        patch("krater.worker.app.get_sessionmaker"),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.kick_quilt_delivery") as mock_kick,
    ):
        mock_grants.release_ready.return_value = 0
        grant_release.func(timestamp=1_791_700_000)

        mock_kick.assert_not_called()


def test_a_failed_run_counts_towards_the_admin_alert() -> None:
    with (
        patch("krater.worker.app.grants") as mock_grants,
        patch("krater.worker.app.get_sessionmaker"),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        mock_grants.release_ready.side_effect = RuntimeError("boom")
        grant_release.func(timestamp=1_791_700_000)

        assert mock_report.call_args.args[0] == "grant_release"
