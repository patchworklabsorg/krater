"""Admin alerts: a task that keeps failing, conditions the watch checks, and what reaches the alerts channel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import GpuPrice, OpsAlert, QuiltOutbox, QuiltOutboxState
from krater.services import ops_alerts
from krater.slack.errors import SlackRequestFailedError
from krater.slack.fake import FakeSlackClient

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
CHANNEL = "C_ALERTS"


def _texts(slack: FakeSlackClient) -> list[str]:
    return [
        m["text"] for (channel, _), m in sorted(slack.messages.items(), key=lambda kv: kv[0][1]) if channel == CHANNEL
    ]


def _fresh_prices(db_session: Session) -> None:
    db_session.add(
        GpuPrice(
            accelerator_name="RTX 3060",
            accelerator_count=1,
            on_demand_min_cents=10,
            on_demand_median_cents=12,
            offer_count=3,
            refreshed_at=NOW - timedelta(hours=1),
        )
    )
    db_session.flush()


def _watch(db_session: Session, slack: FakeSlackClient, now: datetime, settings: Settings | None = None) -> list[str]:
    ops_alerts.check(db_session, settings or Settings(), now=now)
    return ops_alerts.deliver(db_session, slack, CHANNEL, now=now)


def test_a_task_alert_opens_at_its_threshold_reminds_and_clears(db_session: Session) -> None:
    _fresh_prices(db_session)
    slack = FakeSlackClient()
    rule = ops_alerts.FAILURE_RULES["slack"]
    for n in range(rule.threshold - 1):
        ops_alerts.record_failure(db_session, "slack", "SlackRequestFailedError: boom", now=NOW + timedelta(minutes=n))
    assert _watch(db_session, slack, NOW + timedelta(minutes=5)) == []

    ops_alerts.record_failure(db_session, "slack", "slack_invite_team: not_in_channel", now=NOW + timedelta(minutes=6))
    assert _watch(db_session, slack, NOW + timedelta(minutes=7)) == ["slack"]
    (opened,) = _texts(slack)
    assert "Slack tasks keep failing" in opened and "3 failures" in opened and "not_in_channel" in opened

    # Nothing new within the reminder interval, even while it keeps failing.
    ops_alerts.record_failure(db_session, "slack", "again", now=NOW + timedelta(minutes=10))
    assert _watch(db_session, slack, NOW + timedelta(minutes=12)) == []
    ops_alerts.record_failure(db_session, "slack", "again", now=NOW + ops_alerts.RENOTIFY)
    assert _watch(db_session, slack, NOW + ops_alerts.RENOTIFY + timedelta(minutes=8)) == ["slack"]
    assert "(still)" in _texts(slack)[-1]

    # A whole window without a failure clears it, once.
    later = NOW + ops_alerts.RENOTIFY + rule.window + timedelta(minutes=1)
    assert _watch(db_session, slack, later) == ["slack"]
    assert "cleared" in _texts(slack)[-1]
    assert _watch(db_session, slack, later + timedelta(minutes=5)) == []


def test_failures_spread_wider_than_the_window_never_open_an_alert(db_session: Session) -> None:
    rule = ops_alerts.FAILURE_RULES["slack"]
    for n in range(rule.threshold * 2):
        ops_alerts.record_failure(db_session, "slack", "boom", now=NOW + n * (rule.window + timedelta(minutes=1)))

    assert db_session.get(OpsAlert, "slack").opened_at is None


def test_quilts_backlog_and_refused_events_open_an_alert(db_session: Session) -> None:
    _fresh_prices(db_session)
    slack = FakeSlackClient()
    settings = Settings(quilt_url="https://quilt.example")
    db_session.add(
        QuiltOutbox(
            id=uuid.uuid4(),
            type="project.updated",
            external_id="p1",
            payload={},
            occurred_at=NOW - ops_alerts.QUILT_BACKLOG_AGE - timedelta(minutes=1),
        )
    )
    db_session.flush()

    # Without a Quilt URL nothing is sent yet, so waiting events are expected.
    assert _watch(db_session, slack, NOW) == []
    assert _watch(db_session, slack, NOW, settings) == ["quilt_backlog"]
    assert "has waited since" in _texts(slack)[-1]
    db_session.execute(sa.update(QuiltOutbox).values(state=QuiltOutboxState.SENT))
    assert _watch(db_session, slack, NOW + timedelta(minutes=5), settings) == ["quilt_backlog"]
    assert "cleared" in _texts(slack)[-1]


def test_stale_prices_open_an_alert(db_session: Session) -> None:
    slack = FakeSlackClient()

    assert _watch(db_session, slack, NOW) == ["prices_stale"]
    assert "hasn't refreshed ever" in _texts(slack)[-1]


def test_background_jobs_that_end_in_errors_open_an_alert(db_session: Session) -> None:
    _fresh_prices(db_session)
    slack = FakeSlackClient()
    job_id = db_session.scalar(
        sa.text(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status) "
            "VALUES ('default', 'slack_invite_team', 'doing') RETURNING id"
        )
    )
    db_session.execute(
        sa.text("INSERT INTO procrastinate_events (job_id, type, at) VALUES (:id, 'failed', :at)"),
        {"id": job_id, "at": NOW - timedelta(minutes=10)},
    )

    assert _watch(db_session, slack, NOW) == ["failed_jobs"]
    assert "slack_invite_team ×1" in _texts(slack)[-1]
    assert _watch(db_session, slack, NOW + ops_alerts.FAILED_JOBS_WINDOW) == ["failed_jobs"]
    assert "cleared" in _texts(slack)[-1]


def test_without_a_channel_alerts_go_to_the_log(db_session: Session, monkeypatch) -> None:
    logged = []
    monkeypatch.setattr(ops_alerts.logger, "error", lambda msg, *args: logged.append(msg % args))
    ops_alerts.check(db_session, Settings(), now=NOW)

    assert ops_alerts.deliver(db_session, None, None, now=NOW) == ["prices_stale"]
    assert any("GPU prices aren't being refreshed" in line for line in logged)


def test_a_slack_error_leaves_the_alert_for_the_next_run(db_session: Session) -> None:
    class BrokenSlack(FakeSlackClient):
        def post_message(self, channel_id, *, blocks, text):
            raise SlackRequestFailedError("chat.postMessage", "channel_not_found")

    ops_alerts.check(db_session, Settings(), now=NOW)
    assert ops_alerts.deliver(db_session, BrokenSlack(), CHANNEL, now=NOW) == []
    assert ops_alerts.deliver(db_session, FakeSlackClient(), CHANNEL, now=NOW + timedelta(minutes=5)) == [
        "prices_stale"
    ]


def test_the_worker_heartbeat(db_session: Session) -> None:
    assert ops_alerts.worker_alive(db_session, now=NOW) == (False, None)
    ops_alerts.beat(db_session, now=NOW)

    assert ops_alerts.worker_alive(db_session, now=NOW + ops_alerts.HEARTBEAT_STALE) == (True, NOW)
    assert ops_alerts.worker_alive(db_session, now=NOW + ops_alerts.HEARTBEAT_STALE + timedelta(seconds=1))[0] is False
