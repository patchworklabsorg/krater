"""The procrastinate App: background jobs and periodic tasks, backed by the same Postgres database.

Run with: `uv run procrastinate --app=krater.worker.app.app worker`
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import procrastinate

from krater import providers
from krater.compute import launch as compute_launch
from krater.compute import reconcile as compute_reconciler
from krater.compute import scheduler as compute_scheduler
from krater.compute import search_quota as compute_search_quota
from krater.compute import storage_cleanup as compute_storage
from krater.compute.search_pacing import SearchPacer
from krater.config import get_settings
from krater.db import get_sessionmaker
from krater.models import Project, ProjectRevision
from krater.pricing.catalog import CatalogError, get_price_catalog
from krater.quilt import sender as quilt_sender
from krater.services import ops_alerts, people, price_history, slack_notify, slack_reviews
from krater.services.pricing import refresh_prices
from krater.slack import SlackError, get_slack_client
from krater.storage import StorageError, get_compute_store
from krater.vast import get_vast_client
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
    """Log once a minute, and record it for `/healthz/worker`, so it's easy to see the worker and its scheduler are
    alive."""
    logger.info("krater worker heartbeat", extra={"timestamp": timestamp})
    session = get_sessionmaker()()
    try:
        ops_alerts.beat(session, now=datetime.fromtimestamp(timestamp, tz=UTC))
        session.commit()
    finally:
        session.close()


def _report_failure(key: str, message: str) -> None:
    """Count a task's failure towards an admin alert (`krater.services.ops_alerts`), in a session of its own: the
    task's was just rolled back. Never raises: an alert that can't be recorded mustn't hide the failure's own log."""
    session = get_sessionmaker()()
    try:
        ops_alerts.record_failure(session, key, message, now=datetime.now(UTC))
        session.commit()
    except Exception:
        logger.exception("krater: could not record a failure of %s for the admin alerts", key)
        session.rollback()
    finally:
        session.close()


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


@app.periodic(cron="*/5 * * * *")
@app.task(name="ops_watch")
def ops_watch(timestamp: int) -> None:
    """Every five minutes: check for problems and post the admin alerts that changed (`krater.services.ops_alerts`)."""
    settings = get_settings()
    now = max(datetime.now(UTC), datetime.fromtimestamp(timestamp, tz=UTC))
    session = get_sessionmaker()()
    try:
        ops_alerts.check(session, settings, now=now)
        session.commit()
        channel = settings.slack_alerts_channel_id or None
        ops_alerts.deliver(session, get_slack_client() if channel else None, channel, now=now)
        session.commit()
    except Exception:
        logger.exception("krater.ops_watch failed")
        session.rollback()
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
        count = refresh_prices(session, get_price_catalog())
        session.commit()
        logger.info("krater pricing_refresh: %d accelerator/count groups", count)
    except CatalogError:
        logger.exception("krater.pricing_refresh task failed; keeping last known prices")
        session.rollback()
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Krater Compute: Krater's own Vast price history. See `krater.services.price_history`. Only the worker
# holds a Vast client; without a usable one (no key, or the fake in production) this does nothing. It runs with
# compute off too, so the budget estimator has interruptible prices before launching opens; it stops at half of
# Vast's daily search quota, leaving the rest to the scheduler.
# --------------------------------------------------------------------------------------------------


def _price_history_cron() -> str:
    """The cron for `price_history_interval_minutes` (a divisor of 60, or whole hours), built at import time: a
    change takes effect when the worker restarts. Runs at 5 past, away from the hourly tasks."""
    minutes = get_settings().price_history_interval_minutes
    if minutes >= 60:
        hours = minutes // 60
        return "5 * * * *" if hours == 1 else f"5 */{hours} * * *"
    return f"*/{minutes} * * * *"


#: Seconds between price-history searches: 36 searches take about 75 s, leaving the scheduler room under Vast's
#: rate limit.
PRICE_HISTORY_SEARCH_PAUSE_SECONDS = 2.0


@app.periodic(cron=_price_history_cron())
@app.task(name="price_history_record")
def price_history_record(timestamp: int) -> None:
    """Sample Vast's prices for the tracked GPU models. Fails soft: a failed run leaves a gap in the history."""
    settings = get_settings()
    client = get_vast_client()
    if client is None:
        return
    session = get_sessionmaker()()
    try:
        result = price_history.record(
            session,
            client,
            gpu_names=settings.price_history_gpus,
            disk_gb=settings.price_history_disk_gb,
            now=datetime.fromtimestamp(timestamp, tz=UTC),
            # Vast rate-limits about 30 searches in quick succession (staging, 2026-10-11).
            pause_seconds=PRICE_HISTORY_SEARCH_PAUSE_SECONDS,
        )
        compute_search_quota.record(session, client.search_quota, now=datetime.now(UTC))
        session.commit()
        logger.info(
            "krater price_history_record: %d samples, %d failed%s",
            result.written,
            result.failed,
            " (stopped early)" if result.stopped_early else "",
        )
    except Exception as exc:
        logger.exception("krater.price_history_record failed")
        session.rollback()
        _report_failure("price_history", _failure(exc))
    finally:
        session.close()


#: The scheduler's memory of when it last searched for what (`krater.compute.search_pacing`), for this process.
SEARCH_PACER = SearchPacer()

#: Only one scheduler pass runs at a time, so a slow pass can't overlap the next minute's and rent twice.
COMPUTE_SCHEDULER_LOCK = "compute_scheduler"


@app.periodic(cron="* * * * *")
@app.task(name="compute_schedule", lock=COMPUTE_SCHEDULER_LOCK, queueing_lock=COMPUTE_SCHEDULER_LOCK)
def compute_schedule(timestamp: int) -> None:
    """One Krater Compute scheduler pass (`krater.compute.scheduler`). Only with a provider in
    `KRATER_COMPUTE_MODE` and a usable client for it; otherwise nothing is rented or destroyed."""
    settings = get_settings()
    client = providers.get_provider(settings)
    if client is None:
        return
    session = get_sessionmaker()()
    try:
        result = compute_scheduler.run_pass(
            session,
            client,
            settings,
            now=max(datetime.now(UTC), datetime.fromtimestamp(timestamp, tz=UTC)),
            runner_sha256=compute_launch.runner_sha256(settings.compute_runner_binary_path),
            pacer=SEARCH_PACER,
        )
        if result.errors:
            _report_failure("compute_schedule", f"{result.errors} provider errors in one pass; see the worker's log.")
        if result.rented or result.destroyed or result.swept or result.errors:
            logger.info(
                "krater compute_schedule: rented %d, destroyed %d, swept %d, errors %d",
                len(result.rented),
                len(result.destroyed),
                len(result.swept),
                result.errors,
            )
    except Exception as exc:
        logger.exception("krater.compute_schedule failed")
        session.rollback()
        _report_failure("compute_schedule", _failure(exc))
    finally:
        session.close()


@app.periodic(cron="7 * * * *")
@app.task(name="compute_reconcile", lock=COMPUTE_SCHEDULER_LOCK)
def compute_reconcile(timestamp: int) -> None:
    """Hourly: replace Krater Compute's spend estimate with Vast's own charges (`krater.compute.reconcile`). Shares
    the scheduler's lock, since both write instance estimates."""
    del timestamp
    settings = get_settings()
    provider = providers.active(settings)
    if provider is None or not provider.capabilities.charges:
        return  # without the provider's charges, Krater's estimate is the spend
    client = provider.client(settings)
    if client is None:
        return
    session = get_sessionmaker()()
    try:
        result = compute_reconciler.run(session, client, settings, now=datetime.now(UTC))
        logger.info(
            "krater compute_reconcile: %d projects, %d alerts, %d unknown sources",
            result.projects,
            result.alerts,
            len(result.unknown_sources),
        )
    except Exception as exc:
        logger.exception("krater.compute_reconcile failed")
        session.rollback()
        _report_failure("compute_reconcile", _failure(exc))
    finally:
        session.close()


@app.periodic(cron="37 * * * *")
@app.task(name="compute_storage_cleanup")
def compute_storage_cleanup(timestamp: int) -> None:
    """Hourly: abort unfinished multipart uploads of jobs that have ended (`krater.compute.storage_cleanup`)."""
    if providers.active(get_settings()) is None:
        return
    session = get_sessionmaker()()
    try:
        aborted = compute_storage.abort_abandoned_uploads(
            session, get_compute_store(), now=datetime.fromtimestamp(timestamp, tz=UTC)
        )
        if aborted:
            logger.info("krater compute_storage_cleanup: aborted %d unfinished uploads", aborted)
    except StorageError as exc:
        logger.exception("krater.compute_storage_cleanup failed")
        _report_failure("compute_storage_cleanup", _failure(exc))
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Slack: channel/message upkeep, deferred immediately after the web request (or Slack interaction) that
# causes it commits, plus a periodic reconcile for anything missed. See `krater.services.slack_notify`
# and `krater.services.slack_reviews` for the actual logic -- these are thin, self-guarding wrappers
# (each underlying call is a safe-to-retry no-op when it doesn't apply).
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
    except SlackError as exc:
        logger.exception("krater.slack_notify_revision_submitted failed")
        session.rollback()
        _report_failure("slack", f"slack_notify_revision_submitted: {_failure(exc)}")
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
    except SlackError as exc:
        logger.exception("krater.slack_notify_decision failed")
        session.rollback()
        _report_failure("slack", f"slack_notify_decision: {_failure(exc)}")
    finally:
        session.close()


@app.task(name="slack_post_admin_override")
def slack_post_admin_override(
    project_id: str, *, action: str, actor_sub: str, reason: str | None = None, extra: str | None = None
) -> None:
    """Post an admin-override notice to a project's channel. A no-op if it has none yet. The job carries the
    admin's Patchwork ID, not their name, and the name is read from Weave when the notice is posted."""
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
            actor_name=people.lookup(get_weave_client(), actor_sub).label,
            reason=reason,
            extra=extra,
        )
        session.commit()
    except SlackError as exc:
        logger.exception("krater.slack_post_admin_override failed")
        session.rollback()
        _report_failure("slack", f"slack_post_admin_override: {_failure(exc)}")
    finally:
        session.close()


@app.task(name="slack_invite_team")
def slack_invite_team(project_id: str) -> None:
    """Invite a project's team to its channel after someone joins. A no-op while the project has no channel:
    submitting creates it and invites the whole team then."""
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None or project.slack_channel_id is None or project.slack_channel_archived:
            return
        slack_notify.ensure_channel(session, get_slack_client(), get_weave_client(), project=project)
        session.commit()
    except SlackError as exc:
        logger.exception("krater.slack_invite_team failed")
        session.rollback()
        _report_failure("slack", f"slack_invite_team: {_failure(exc)}")
    finally:
        session.close()


@app.task(name="slack_remove_from_channel")
def slack_remove_from_channel(project_id: str, user_id: str) -> None:
    """Take someone who left a project's team out of its channel (`slack_notify.remove_from_channel`)."""
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            return
        slack_notify.remove_from_channel(
            session, get_slack_client(), get_weave_client(), project=project, user_id=uuid.UUID(user_id)
        )
        session.commit()
    except SlackError as exc:
        logger.exception("krater.slack_remove_from_channel failed")
        session.rollback()
        _report_failure("slack", f"slack_remove_from_channel: {_failure(exc)}")
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
    except SlackError as exc:
        logger.exception("krater.slack_archive_channel failed")
        session.rollback()
        _report_failure("slack", f"slack_archive_channel: {_failure(exc)}")
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
    except SlackError as exc:
        logger.exception("krater.slack_process_approve failed")
        session.rollback()
        _report_failure("slack", f"slack_process_approve: {_failure(exc)}")
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
    except SlackError as exc:
        logger.exception("krater.slack_process_reject failed")
        session.rollback()
        _report_failure("slack", f"slack_process_reject: {_failure(exc)}")
    finally:
        session.close()


def _slack_reconcile_cron() -> str:
    """`*/N * * * *` from `slack_reconcile_interval_minutes`, built at import time: a change
    takes effect when the worker restarts."""
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
    except SlackError as exc:
        # `reconcile` already catches per-step Slack errors and logs+continues; this is a last-resort net.
        logger.exception("krater.slack_reconcile task failed")
        _report_failure("slack", f"slack_reconcile: {_failure(exc)}")
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
        if result is not None and result.failed:
            _report_failure("quilt", f"Quilt refused {result.failed} events for good; see the outbox on /admin.")
        if result is not None and (result.sent or result.retried or result.failed):
            logger.info(
                "krater quilt_deliver: sent %d, retry %d, failed %d",
                result.sent,
                result.retried,
                result.failed,
            )
    except Exception as exc:
        logger.exception("krater.quilt_deliver failed")
        session.rollback()
        _report_failure("quilt", _failure(exc))
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
