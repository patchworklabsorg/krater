"""The `skypilot_reconcile` periodic task: registered on the procrastinate app, on the schedule
`skypilot_reconcile_interval_minutes` implies, and calling through to `skypilot_sync.reconcile`.
"""

from __future__ import annotations

from unittest.mock import patch

from krater.worker.app import app, skypilot_reconcile


def test_skypilot_reconcile_is_registered_as_a_periodic_task() -> None:
    assert "skypilot_reconcile" in app.tasks

    periodic_task = app.periodic_registry.periodic_tasks[("skypilot_reconcile", "")]
    # `KRATER_SKYPILOT_RECONCILE_INTERVAL_MINUTES` defaults to 5 (`krater/config.py`).
    assert periodic_task.cron == "*/5 * * * *"


def test_skypilot_reconcile_calls_through_to_the_service_function() -> None:
    with (
        patch("krater.worker.app.reconcile") as mock_reconcile,
        patch("krater.worker.app.get_skypilot_client") as mock_get_client,
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
        patch("krater.worker.app.get_weave_client") as mock_get_weave_client,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value
        fake_client = mock_get_client.return_value

        skypilot_reconcile.func(timestamp=123)

        mock_reconcile.assert_called_once()
        args, kwargs = mock_reconcile.call_args
        assert args[0] is fake_session
        assert args[1] is fake_client
        assert args[2] is mock_get_weave_client.return_value
        assert kwargs["warn_percent"] == 80  # KRATER_SKYPILOT_BUDGET_WARN_PERCENT default
        fake_session.close.assert_called_once()
