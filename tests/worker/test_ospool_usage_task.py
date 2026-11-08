"""The daily OSPool usage import (`krater.worker.app.ospool_usage_import`)."""

from __future__ import annotations

from unittest.mock import patch

from krater.config import get_settings
from krater.worker.app import OSPOOL_USAGE_LOCK, app, ospool_usage_import


def test_it_runs_daily_under_a_lock() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("ospool_usage_import", "")]

    assert periodic_task.cron == get_settings().ospool_usage_import_cron == "41 8 * * *"
    assert ospool_usage_import.lock == OSPOOL_USAGE_LOCK
    assert ospool_usage_import.queueing_lock == OSPOOL_USAGE_LOCK


def test_grants_osg_couldnt_answer_for_count_towards_an_alert() -> None:
    with (
        patch("krater.worker.app.ospool_grants") as mock_grants,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
        patch("krater.worker.app.get_accounting_client") as mock_client,
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        session = mock_sessionmaker.return_value.return_value
        mock_grants.import_all.return_value = ["PatchworkLabs_Ganymede01"]
        ospool_usage_import.func(timestamp=1_791_700_000)

        args, kwargs = mock_grants.import_all.call_args
        assert args == (session, mock_client.return_value) and "now" in kwargs
        key, message = mock_report.call_args.args
        assert key == "ospool_accounting"
        assert "PatchworkLabs_Ganymede01" in message
        session.close.assert_called_once()


def test_a_clean_run_reports_nothing() -> None:
    with (
        patch("krater.worker.app.ospool_grants") as mock_grants,
        patch("krater.worker.app.get_sessionmaker"),
        patch("krater.worker.app.get_accounting_client"),
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        mock_grants.import_all.return_value = []
        ospool_usage_import.func(timestamp=1_791_700_000)

        mock_report.assert_not_called()
