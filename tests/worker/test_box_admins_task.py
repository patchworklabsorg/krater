"""The `box_admins_sync` periodic task: every five minutes, under a lock, whatever KRATER_COMPUTE_MODE says."""

from __future__ import annotations

from unittest.mock import patch

from krater.config import get_settings
from krater.worker.app import BOX_ADMINS_LOCK, app, box_admins_sync


def test_box_admins_sync_runs_every_five_minutes_under_a_lock() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("box_admins_sync", "")]

    assert periodic_task.cron == "*/5 * * * *"
    assert box_admins_sync.lock == BOX_ADMINS_LOCK
    assert box_admins_sync.queueing_lock == BOX_ADMINS_LOCK


def test_box_admins_sync_runs_with_compute_off_and_commits(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    with (
        patch("krater.worker.app.box_access") as mock_box_access,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
        patch("krater.worker.app.get_weave_client") as mock_weave,
    ):
        session = mock_sessionmaker.return_value.return_value
        box_admins_sync.func(timestamp=1_791_700_000)

        args, kwargs = mock_box_access.sync_admins.call_args
        assert args == (session, mock_weave.return_value) and "now" in kwargs
        session.commit.assert_called_once()
        session.close.assert_called_once()


def test_a_failed_sync_rolls_back_and_counts_towards_the_admin_alert() -> None:
    with (
        patch("krater.worker.app.box_access") as mock_box_access,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        session = mock_sessionmaker.return_value.return_value
        mock_box_access.sync_admins.side_effect = RuntimeError("Weave is down")
        box_admins_sync.func(timestamp=1_791_700_000)

        session.rollback.assert_called_once()
        session.commit.assert_not_called()
        assert mock_report.call_args.args[0] == "box_admins"
