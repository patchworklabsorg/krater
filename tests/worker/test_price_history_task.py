"""The `price_history_record` periodic task: registered on its schedule, a no-op with compute off or without a Vast
client."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from krater.config import get_settings
from krater.worker import app as worker_app
from krater.worker.app import app, price_history_record


@pytest.fixture(autouse=True)
def compute_on():
    settings = get_settings().model_copy(update={"compute_mode": "vast"})
    with patch("krater.worker.app.get_settings", return_value=settings):
        yield settings


def test_price_history_record_is_registered_hourly() -> None:
    # Each run costs about 500 of Vast's daily search rows (docs/compute/vast-api.md).
    periodic_task = app.periodic_registry.periodic_tasks[("price_history_record", "")]

    assert periodic_task.cron == "5 * * * *"


@pytest.mark.parametrize(("minutes", "cron"), [(10, "*/10 * * * *"), (60, "5 * * * *"), (180, "5 */3 * * *")])
def test_the_cron_follows_the_interval(compute_on, minutes: int, cron: str) -> None:
    with patch(
        "krater.worker.app.get_settings",
        return_value=compute_on.model_copy(update={"price_history_interval_minutes": minutes}),
    ):
        assert worker_app._price_history_cron() == cron


def test_price_history_record_does_nothing_without_a_vast_client() -> None:
    with (
        patch("krater.worker.app.get_vast_client", return_value=None),
        patch("krater.worker.app.price_history") as mock_history,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        price_history_record.func(timestamp=1_791_700_000)

        mock_history.record.assert_not_called()
        mock_sessionmaker.assert_not_called()


def test_price_history_record_records_and_commits() -> None:
    with (
        patch("krater.worker.app.get_vast_client") as mock_client,
        patch("krater.worker.app.price_history") as mock_history,
        patch("krater.worker.app.compute_search_quota") as mock_quota,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value
        price_history_record.func(timestamp=1_791_700_000)

        args, kwargs = mock_history.record.call_args
        assert args == (session, mock_client.return_value)
        assert kwargs["disk_gb"] == 32
        assert "RTX 4090" in kwargs["gpu_names"]
        assert kwargs["now"].timestamp() == 1_791_700_000
        mock_quota.record.assert_called_once()  # saved for /admin/compute
        session.commit.assert_called_once()
        session.close.assert_called_once()
