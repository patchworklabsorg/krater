"""Thin wiring tests for the Quilt procrastinate tasks (`krater/worker/app.py`): registration, the
schedule, the call through to `krater.quilt.sender.run`, and the post-commit kick. The sender itself is
tested in `tests/quilt/`."""

from __future__ import annotations

from unittest.mock import patch

import procrastinate

from krater.config import Settings
from krater.worker import app as worker_module
from krater.worker.app import app, kick_quilt_delivery, quilt_deliver, quilt_deliver_now


def test_quilt_deliver_runs_every_minute() -> None:
    assert "quilt_deliver" in app.tasks
    assert app.periodic_registry.periodic_tasks[("quilt_deliver", "")].cron == "* * * * *"


def test_the_kick_task_has_a_queueing_lock() -> None:
    assert quilt_deliver_now.queueing_lock == worker_module.QUILT_KICK_LOCK


def test_both_tasks_call_through_to_the_sender() -> None:
    with (
        patch("krater.worker.app.quilt_sender.run") as mock_run,
        patch("krater.worker.app.get_weave_client") as mock_weave,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value
        mock_run.return_value = None

        quilt_deliver.func(timestamp=1)
        quilt_deliver_now.func()

        assert mock_run.call_count == 2
        assert mock_run.call_args.args[:2] == (session, mock_weave.return_value)
        assert session.close.call_count == 2


def test_a_sender_crash_is_logged_and_rolled_back() -> None:
    with (
        patch("krater.worker.app.quilt_sender.run", side_effect=RuntimeError("boom")),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value

        quilt_deliver.func(timestamp=1)

        session.rollback.assert_called_once()
        session.close.assert_called_once()


def test_the_kick_does_nothing_without_a_quilt_url() -> None:
    with (
        patch("krater.worker.app.get_settings", return_value=Settings(quilt_url="")),
        patch.object(quilt_deliver_now, "defer") as mock_defer,
    ):
        kick_quilt_delivery()

    mock_defer.assert_not_called()


def test_the_kick_defers_when_quilt_is_configured_and_never_raises() -> None:
    settings = Settings(quilt_url="https://quilt.test")
    with (
        patch("krater.worker.app.get_settings", return_value=settings),
        patch.object(quilt_deliver_now, "defer") as mock_defer,
    ):
        kick_quilt_delivery()
        mock_defer.assert_called_once_with()

        mock_defer.side_effect = procrastinate.exceptions.AlreadyEnqueued("waiting")
        kick_quilt_delivery()

        mock_defer.side_effect = RuntimeError("db down")
        kick_quilt_delivery()
