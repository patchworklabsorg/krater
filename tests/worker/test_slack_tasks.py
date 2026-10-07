"""Thin wiring tests for the Slack procrastinate tasks (`krater/worker/app.py`): registration, the
periodic schedule, and that each task calls through to the right service function. Mirrors
`tests/worker/test_skypilot_reconcile.py` -- the actual logic is unit-tested in `tests/services/`.
"""

from __future__ import annotations

from unittest.mock import patch

from krater.worker.app import (
    app,
    slack_archive_channel,
    slack_notify_decision,
    slack_notify_revision_submitted,
    slack_post_admin_override,
    slack_process_approve,
    slack_process_reject,
    slack_reconcile,
)


def test_slack_reconcile_is_registered_as_a_periodic_task() -> None:
    assert "slack_reconcile" in app.tasks
    periodic_task = app.periodic_registry.periodic_tasks[("slack_reconcile", "")]
    # `KRATER_SLACK_RECONCILE_INTERVAL_MINUTES` defaults to 10 (`krater/config.py`).
    assert periodic_task.cron == "*/10 * * * *"


def test_slack_reconcile_calls_through_to_the_service_function() -> None:
    with (
        patch("krater.worker.app.slack_notify.reconcile") as mock_reconcile,
        patch("krater.worker.app.get_slack_client") as mock_get_slack_client,
        patch("krater.worker.app.get_weave_client") as mock_get_weave_client,
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        slack_reconcile.func(timestamp=123)

        mock_reconcile.assert_called_once_with(
            fake_session, mock_get_slack_client.return_value, mock_get_weave_client.return_value
        )
        fake_session.close.assert_called_once()


def test_slack_notify_revision_submitted_looks_up_and_calls_through() -> None:
    with (
        patch("krater.worker.app.slack_notify.notify_revision_submitted") as mock_notify,
        patch("krater.worker.app.get_slack_client"),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value
        fake_revision = fake_session.get.return_value

        slack_notify_revision_submitted.func(revision_id="00000000-0000-0000-0000-000000000001")

        fake_session.get.assert_called_once()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["revision"] is fake_revision
        fake_session.commit.assert_called_once()


def test_slack_notify_revision_submitted_is_a_no_op_when_the_revision_is_gone() -> None:
    with (
        patch("krater.worker.app.slack_notify.notify_revision_submitted") as mock_notify,
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value
        fake_session.get.return_value = None

        slack_notify_revision_submitted.func(revision_id="00000000-0000-0000-0000-000000000001")

        mock_notify.assert_not_called()
        fake_session.close.assert_called_once()


def test_slack_notify_decision_calls_through() -> None:
    with (
        patch("krater.worker.app.slack_notify.notify_decision") as mock_notify,
        patch("krater.worker.app.get_slack_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        slack_notify_decision.func(revision_id="00000000-0000-0000-0000-000000000001")

        mock_notify.assert_called_once()
        fake_session.commit.assert_called_once()


def test_slack_post_admin_override_calls_through() -> None:
    with (
        patch("krater.worker.app.slack_notify.post_admin_override") as mock_notify,
        patch("krater.worker.app.get_slack_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        slack_post_admin_override.func(
            project_id="00000000-0000-0000-0000-000000000001", action="admin_approve", actor_name="Ana", reason="why"
        )

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["action"] == "admin_approve"
        fake_session.commit.assert_called_once()


def test_slack_archive_channel_calls_through() -> None:
    with (
        patch("krater.worker.app.slack_notify.archive_project_channel") as mock_archive,
        patch("krater.worker.app.get_slack_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        slack_archive_channel.func(project_id="00000000-0000-0000-0000-000000000001")

        mock_archive.assert_called_once()
        fake_session.commit.assert_called_once()


def test_slack_process_approve_calls_through() -> None:
    with (
        patch("krater.worker.app.slack_reviews.process_approve") as mock_process,
        patch("krater.worker.app.get_slack_client"),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        slack_process_approve.func(
            revision_id="00000000-0000-0000-0000-000000000001",
            slack_user_id="U1",
            response_url="https://hooks.example/1",
        )

        mock_process.assert_called_once()
        assert mock_process.call_args.args[0] is fake_session
        fake_session.close.assert_called_once()


def test_slack_process_reject_calls_through() -> None:
    with (
        patch("krater.worker.app.slack_reviews.process_reject") as mock_process,
        patch("krater.worker.app.get_slack_client"),
        patch("krater.worker.app.get_weave_client"),
        patch("krater.worker.app.get_sessionmaker") as mock_get_sessionmaker,
    ):
        fake_session = mock_get_sessionmaker.return_value.return_value

        slack_process_reject.func(
            revision_id="00000000-0000-0000-0000-000000000001",
            slack_user_id="U1",
            reason="no",
            response_url="https://hooks.example/1",
        )

        mock_process.assert_called_once()
        fake_session.close.assert_called_once()
