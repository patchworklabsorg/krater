"""The `pricing_refresh` periodic task: registered on the procrastinate app, on the configured daily
schedule, calling through to `krater.services.pricing.refresh_prices`, and failing soft on a source
error (mirrors `tests/worker/test_skypilot_reconcile.py`)."""

from __future__ import annotations

from unittest.mock import patch

from krater.skypilot import SkyPilotUnavailableError
from krater.worker.app import app, pricing_refresh


def test_pricing_refresh_is_registered_as_a_periodic_task() -> None:
    assert "pricing_refresh" in app.tasks

    periodic_task = app.periodic_registry.periodic_tasks[("pricing_refresh", "")]
    # `KRATER_PRICING_REFRESH_CRON` defaults to "0 7 * * *" (krater/config.py).
    assert periodic_task.cron == "0 7 * * *"


def test_pricing_refresh_calls_through_to_the_service_function() -> None:
    with (
        patch("krater.worker.app.refresh_prices") as mock_refresh_prices,
        patch("krater.worker.app.get_skypilot_client") as mock_get_client,
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value
        fake_client = mock_get_client.return_value
        mock_refresh_prices.return_value = 3

        pricing_refresh.func(timestamp=123)

        mock_refresh_prices.assert_called_once_with(fake_session, fake_client)
        fake_session.commit.assert_called_once()
        fake_session.close.assert_called_once()


def test_pricing_refresh_fails_soft_on_a_source_error() -> None:
    with (
        patch("krater.worker.app.refresh_prices", side_effect=SkyPilotUnavailableError("down")),
        patch("krater.worker.app.get_skypilot_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        pricing_refresh.func(timestamp=123)  # must not raise

        fake_session.commit.assert_not_called()
        fake_session.rollback.assert_called_once()
        fake_session.close.assert_called_once()
