"""The procrastinate App: background jobs and periodic tasks, backed by the same Postgres database.

Run with: `uv run procrastinate --app=krater.worker.app.app worker`
"""

from __future__ import annotations

import logging
import uuid

import procrastinate

from krater.config import get_settings
from krater.db import get_sessionmaker
from krater.models import Project, ProjectRevision
from krater.quilt import sender as quilt_sender
from krater.services import slack_notify, slack_reviews
from krater.services.pricing import refresh_prices
from krater.services.skypilot_sync import reconcile
from krater.skypilot import SkyPilotError, get_skypilot_client
from krater.slack import SlackError, get_slack_client
from krater.weave import get_weave_client

logger = logging.getLogger(__name__)


def _psycopg_conninfo(database_url: str) -> str:
    """Turn a SQLAlchemy-style URL (`postgresql+psycopg://...`) into a plain libpq conninfo string."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


app = procrastinate.App(
    # `min_size`/`max_size`: the web app also opens this same connector (see `krater.web.app`'s
    # lifespan) purely to defer jobs, which needs far fewer connections than a worker actually running
    # tasks -- kept small so `create_app()` (and every test that builds one) doesn't reserve a large
    # pool it barely uses.
    connector=procrastinate.PsycopgConnector(
        conninfo=_psycopg_conninfo(get_settings().database_url), min_size=1, max_size=5
    ),
)


@app.periodic(cron="* * * * *")
@app.task(name="heartbeat")
def heartbeat(timestamp: int) -> None:
    """Log once a minute, so it's easy to see the worker and its scheduler are alive."""
    logger.info("krater worker heartbeat", extra={"timestamp": timestamp})


def _skypilot_reconcile_cron() -> str:
    """`*/N * * * *` from `skypilot_reconcile_interval_minutes` -- built at import time, so changing
    the setting takes effect the next time the worker (and its scheduler) restarts."""
    return f"*/{get_settings().skypilot_reconcile_interval_minutes} * * * *"


@app.periodic(cron=_skypilot_reconcile_cron())
@app.task(name="skypilot_reconcile")
def skypilot_reconcile(timestamp: int) -> None:
    """Provision/tear down SkyPilot workspaces, take spend snapshots, and enforce budgets.

    Thin wrapper around `krater.services.skypilot_sync.reconcile`; see there for the actual logic. Also
    runnable directly for manual runs/debugging via `python -m krater.skypilot.reconcile_once`.
    """
    del timestamp
    settings = get_settings()
    session = get_sessionmaker()()
    try:
        reconcile(
            session, get_skypilot_client(), get_weave_client(), warn_percent=settings.skypilot_budget_warn_percent
        )
    except SkyPilotError:
        # `reconcile` already catches per-step SkyPilot errors and logs+continues; this is a last-resort
        # net for anything that still escapes (e.g. a step raising before its own try/except is reached).
        logger.exception("krater.skypilot_reconcile task failed")
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# GPU pricing: a daily refresh of the aggregated Vast catalog `/pricing` and the budget estimator read.
# See `krater.services.pricing` and `docs/dev/pricing.md`. Also runnable directly for a manual refresh
# via `python -m krater.pricing.refresh_once`.
# --------------------------------------------------------------------------------------------------


@app.periodic(cron=get_settings().pricing_refresh_cron)
@app.task(name="pricing_refresh")
def pricing_refresh(timestamp: int) -> None:
    """Fetch, aggregate and atomically replace `GpuPrice`. Fails soft: on a source error, this logs and
    leaves the last successfully-refreshed prices in place (see `refresh_prices`'s docstring)."""
    del timestamp
    session = get_sessionmaker()()
    try:
        count = refresh_prices(session, get_skypilot_client())
        session.commit()
        logger.info("krater pricing_refresh: %d accelerator/count groups", count)
    except SkyPilotError:
        logger.exception("krater.pricing_refresh task failed; keeping last known prices")
        session.rollback()
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Slack: channel/message upkeep, deferred immediately after the web request (or Slack interaction) that
# causes it commits, plus a periodic reconcile for anything missed. See `krater.services.slack_notify`
# and `krater.services.slack_reviews` for the actual logic -- these are thin, self-guarding wrappers
# (each underlying call is a safe-to-retry no-op when it doesn't apply), mirroring `skypilot_reconcile`.
# --------------------------------------------------------------------------------------------------


@app.task(name="slack_notify_revision_submitted")
def slack_notify_revision_submitted(revision_id: str) -> None:
    """Ensure the project's channel exists, invite the team, and post the review message. Deferred
    from `krater.web.routers.projects` right after a proposal/amendment/completion submission commits.
    """
    settings = get_settings()
    session = get_sessionmaker()()
    try:
        revision = session.get(ProjectRevision, uuid.UUID(revision_id))
        if revision is None:
            return
        slack_notify.notify_revision_submitted(
            session,
            get_slack_client(),
            get_weave_client(),
            revision=revision,
            feed_channel_id=settings.slack_feed_channel_id or None,
        )
        session.commit()
    except SlackError:
        logger.exception("krater.slack_notify_revision_submitted failed")
        session.rollback()
    finally:
        session.close()


@app.task(name="slack_notify_decision")
def slack_notify_decision(revision_id: str) -> None:
    """Update the review message to show a revision's decided outcome. A no-op if the revision is
    still pending -- see `krater.services.slack_notify.notify_decision` -- so it's safe to defer
    unconditionally after any decision (web, Slack, or admin override)."""
    session = get_sessionmaker()()
    try:
        revision = session.get(ProjectRevision, uuid.UUID(revision_id))
        if revision is None:
            return
        slack_notify.notify_decision(session, get_slack_client(), revision=revision)
        session.commit()
    except SlackError:
        logger.exception("krater.slack_notify_decision failed")
        session.rollback()
    finally:
        session.close()


@app.task(name="slack_post_admin_override")
def slack_post_admin_override(
    project_id: str, *, action: str, actor_name: str, reason: str | None = None, extra: str | None = None
) -> None:
    """Post an admin-override notice to a project's channel. A no-op if it has none yet."""
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            return
        slack_notify.post_admin_override(
            session,
            get_slack_client(),
            project=project,
            action=action,
            actor_name=actor_name,
            reason=reason,
            extra=extra,
        )
        session.commit()
    except SlackError:
        logger.exception("krater.slack_post_admin_override failed")
        session.rollback()
    finally:
        session.close()


@app.task(name="slack_archive_channel")
def slack_archive_channel(project_id: str) -> None:
    """Archive a project's channel. A no-op unless the project is actually `completed`/`withdrawn` --
    see `krater.services.slack_notify.archive_project_channel` -- so it's safe to defer unconditionally
    after every decision/withdrawal, whether or not it actually finished the project."""
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            return
        slack_notify.archive_project_channel(session, get_slack_client(), project=project)
        session.commit()
    except SlackError:
        logger.exception("krater.slack_archive_channel failed")
        session.rollback()
    finally:
        session.close()


@app.task(name="slack_process_approve")
def slack_process_approve(revision_id: str, slack_user_id: str, response_url: str) -> None:
    """Handle a Slack Approve button click. Deferred from `POST /slack/interactions` so the route
    itself can ack within Slack's 3s window."""
    session = get_sessionmaker()()
    try:
        slack_reviews.process_approve(
            session,
            get_slack_client(),
            get_weave_client(),
            revision_id=uuid.UUID(revision_id),
            slack_user_id=slack_user_id,
            response_url=response_url,
        )
        kick_quilt_delivery()
    except SlackError:
        logger.exception("krater.slack_process_approve failed")
        session.rollback()
    finally:
        session.close()


@app.task(name="slack_process_reject")
def slack_process_reject(revision_id: str, slack_user_id: str, reason: str, response_url: str) -> None:
    """Handle a Slack reject-modal submission. Deferred from `POST /slack/interactions`."""
    session = get_sessionmaker()()
    try:
        slack_reviews.process_reject(
            session,
            get_slack_client(),
            get_weave_client(),
            revision_id=uuid.UUID(revision_id),
            slack_user_id=slack_user_id,
            reason=reason,
            response_url=response_url,
        )
        kick_quilt_delivery()
    except SlackError:
        logger.exception("krater.slack_process_reject failed")
        session.rollback()
    finally:
        session.close()


def _slack_reconcile_cron() -> str:
    """`*/N * * * *` from `slack_reconcile_interval_minutes` -- see `_skypilot_reconcile_cron` above."""
    return f"*/{get_settings().slack_reconcile_interval_minutes} * * * *"


@app.periodic(cron=_slack_reconcile_cron())
@app.task(name="slack_reconcile")
def slack_reconcile(timestamp: int) -> None:
    """Invite newly-added reviewers to open project channels, post any missed budget warning/teardown
    notifications, and archive any finished project's channel that was missed. Thin wrapper around
    `krater.services.slack_notify.reconcile`."""
    del timestamp
    session = get_sessionmaker()()
    try:
        slack_notify.reconcile(session, get_slack_client(), get_weave_client())
    except SlackError:
        # `reconcile` already catches per-step Slack errors and logs+continues; last-resort net, as in
        # `skypilot_reconcile` above.
        logger.exception("krater.slack_reconcile task failed")
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Quilt: send the `quilt_outbox` rows the services wrote. A periodic run every minute, plus a kick
# deferred right after a web action commits, so events usually reach Quilt within seconds. See
# `krater.quilt.sender` and docs/quilt-integration.md.
# --------------------------------------------------------------------------------------------------

#: Only one kick waits in the queue at a time; more kicks while one waits add nothing.
QUILT_KICK_LOCK = "quilt_deliver_now"


def _quilt_deliver() -> None:
    session = get_sessionmaker()()
    try:
        result = quilt_sender.run(session, get_weave_client(), get_settings())
        if result is not None and (result.sent or result.retried or result.failed):
            logger.info(
                "krater quilt_deliver: sent %d, retry %d, failed %d",
                result.sent,
                result.retried,
                result.failed,
            )
    except Exception:
        logger.exception("krater.quilt_deliver failed")
        session.rollback()
    finally:
        session.close()


@app.periodic(cron="* * * * *")
@app.task(name="quilt_deliver")
def quilt_deliver(timestamp: int) -> None:
    """Send due outbox rows to Quilt. A no-op while `KRATER_QUILT_URL` is blank."""
    del timestamp
    _quilt_deliver()


@app.task(name="quilt_deliver_now", queueing_lock=QUILT_KICK_LOCK)
def quilt_deliver_now() -> None:
    """The same as `quilt_deliver`, deferred after a web action commits."""
    _quilt_deliver()


def kick_quilt_delivery() -> None:
    """Defer `quilt_deliver_now`, if Quilt is configured. Never fails the caller: the periodic run is
    the safety net."""
    if not get_settings().quilt_url:
        return
    try:
        quilt_deliver_now.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        pass
    except Exception:
        logger.warning("krater: could not defer quilt_deliver_now; the periodic run sends the events", exc_info=True)
