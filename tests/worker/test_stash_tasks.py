"""Thin wiring tests for the Stash procrastinate tasks (`krater/worker/app.py`): registration, the schedule,
the call through to `krater.stash.sender.run`, and the post-commit kick. The sender itself is tested in
`tests/stash/`."""

from __future__ import annotations

from unittest.mock import patch

import procrastinate
import pytest

from krater.config import Settings
from krater.stash.sender import DeliveryResult
from krater.worker import app as worker_module
from krater.worker.app import app, kick_stash_archiving, stash_archive, stash_archive_now


def test_stash_archive_runs_every_minute() -> None:
    assert "stash_archive" in app.tasks
    assert app.periodic_registry.periodic_tasks[("stash_archive", "")].cron == "* * * * *"


def test_the_kick_task_has_a_queueing_lock() -> None:
    assert stash_archive_now.queueing_lock == worker_module.STASH_KICK_LOCK


def test_both_tasks_call_through_to_the_sender() -> None:
    with (
        patch("krater.worker.app.stash_sender.run") as mock_run,
        patch("krater.worker.app.get_weave_client") as mock_weave,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value
        mock_run.return_value = None

        stash_archive.func(timestamp=1)
        stash_archive_now.func()

        assert mock_run.call_count == 2
        assert mock_run.call_args.args[:2] == (session, mock_weave.return_value)
        assert session.close.call_count == 2


def test_a_sender_crash_is_logged_and_rolled_back() -> None:
    with (
        patch("krater.worker.app.stash_sender.run", side_effect=RuntimeError("boom")),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        session = mock_sessionmaker.return_value.return_value

        stash_archive.func(timestamp=1)

        session.rollback.assert_called_once()
        session.close.assert_called_once()
        # Counted towards the admins' alert for Stash.
        mock_report.assert_called_once_with("stash", "RuntimeError: boom")


@pytest.mark.parametrize(
    ("stopped", "says"),
    [
        ("no_token", "allows the `stash` scope"),
        ("http_403", "Stash answered http_403"),
        ("http_404", "KRATER_STASH_URL"),
    ],
)
def test_a_run_stopped_by_the_setup_counts_towards_the_alert(stopped: str, says: str) -> None:
    with (
        patch("krater.worker.app.stash_sender.run", return_value=DeliveryResult(stopped=stopped)),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker"),
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        stash_archive.func(timestamp=1)

    [(key, message)] = [call.args for call in mock_report.call_args_list]
    assert key == "stash"
    assert says in message


@pytest.mark.parametrize("result", [DeliveryResult(archived=3), DeliveryResult(stopped="rate_limited", retried=1)])
def test_a_normal_run_reports_nothing(result: DeliveryResult) -> None:
    with (
        patch("krater.worker.app.stash_sender.run", return_value=result),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker"),
        patch("krater.worker.app._report_failure") as mock_report,
    ):
        stash_archive.func(timestamp=1)

    mock_report.assert_not_called()


def test_the_kick_does_nothing_without_a_stash_url() -> None:
    with (
        patch("krater.worker.app.get_settings", return_value=Settings(stash_url="")),
        patch.object(stash_archive_now, "defer") as mock_defer,
    ):
        kick_stash_archiving()

    mock_defer.assert_not_called()


def test_the_kick_defers_when_stash_is_configured_and_never_raises() -> None:
    settings = Settings(stash_url="https://stash.test")
    with (
        patch("krater.worker.app.get_settings", return_value=settings),
        patch.object(stash_archive_now, "defer") as mock_defer,
    ):
        kick_stash_archiving()
        mock_defer.assert_called_once_with()

        mock_defer.side_effect = procrastinate.exceptions.AlreadyEnqueued("waiting")
        kick_stash_archiving()

        mock_defer.side_effect = RuntimeError("db down")
        kick_stash_archiving()
