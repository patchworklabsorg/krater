"""The sender: delivers `quilt_outbox` rows to Quilt, oldest first, one row per transaction.

Rules (see `docs/quilt-integration.md`, "Delivery"):

- Order per subject. A row is sent only when no earlier row of the same `external_id` is still `pending`
  or `failed`. So when a row fails, the later rows of that subject wait, in this run and after it.
- Two workers never send the same row: each row is taken with `SELECT ... FOR UPDATE SKIP LOCKED`, and a
  row that another worker holds still blocks the later rows of its subject.
- 201 and 200 mean sent. 409 `unknown_submission`, 5xx and network errors are retried with backoff.
  401 and 403 are retried too, but they are a configuration error: the run stops and logs it.
  409 `id_conflict` / `submission_exists`, 413 and 422 fail for good: an admin sees them on `/admin`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from krater.config import Settings
from krater.models import QuiltOutbox, QuiltOutboxState
from krater.quilt.client import QuiltClient, QuiltResponse
from krater.weave import WeaveClient, WeaveUnavailableError

logger = logging.getLogger(__name__)

#: The first retry waits this long; each later one waits twice as long, up to `MAX_BACKOFF`.
BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(hours=1)

#: Quilt's 409 errors that mean "send it again later" (the submission isn't there yet).
_RETRYABLE_CONFLICTS = frozenset({"unknown_submission"})


class Outcome(Enum):
    SENT = "sent"
    RETRY = "retry"
    RETRY_CONFIG = "retry_config"  # 401/403: retry later, but stop this run, and tell an operator
    FAILED = "failed"


def classify(response: QuiltResponse) -> Outcome:
    """What one answer from Quilt means for the row."""
    status = response.status
    if status in (200, 201):
        return Outcome.SENT
    if status is None or status >= 500:
        return Outcome.RETRY
    if status in (401, 403):
        return Outcome.RETRY_CONFIG
    if status == 409:
        return Outcome.RETRY if response.error in _RETRYABLE_CONFLICTS else Outcome.FAILED
    if status in (413, 422):
        return Outcome.FAILED
    # Anything else (a 404 from a wrong KRATER_QUILT_URL, a 400 from a proxy) is not the event's fault.
    return Outcome.RETRY_CONFIG


def backoff(attempts: int) -> timedelta:
    """How long to wait after the `attempts`-th failed attempt."""
    return min(BASE_BACKOFF * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)


@dataclass
class DeliveryResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    stopped: str | None = None  # why the run stopped early, if it did


def _utcnow() -> datetime:
    return datetime.now(UTC)


def next_row(session: Session, now: datetime) -> QuiltOutbox | None:
    """Lock and return the oldest row that may be sent now, or `None`."""
    earlier = aliased(QuiltOutbox)
    blocked = (
        sa.exists()
        .where(
            earlier.external_id == QuiltOutbox.external_id,
            earlier.seq < QuiltOutbox.seq,
            earlier.state.in_((QuiltOutboxState.PENDING, QuiltOutboxState.FAILED)),
        )
        .correlate(QuiltOutbox)
    )
    stmt = (
        sa.select(QuiltOutbox)
        .where(
            QuiltOutbox.state == QuiltOutboxState.PENDING,
            sa.or_(QuiltOutbox.next_attempt_at.is_(None), QuiltOutbox.next_attempt_at <= now),
            ~blocked,
        )
        .order_by(QuiltOutbox.seq)
        .limit(1)
        .with_for_update(skip_locked=True, of=QuiltOutbox)
    )
    return session.scalars(stmt).first()


def _record(row: QuiltOutbox, response: QuiltResponse, outcome: Outcome, now: datetime) -> None:
    row.attempts += 1
    row.last_status = response.status
    if outcome is Outcome.SENT:
        row.state = QuiltOutboxState.SENT
        row.sent_at = now
        row.next_attempt_at = None
        row.last_error = None
        return
    row.last_error = response.error or (response.body[:500] if response.body else None)
    if outcome is Outcome.FAILED:
        row.state = QuiltOutboxState.FAILED
        row.next_attempt_at = None
        logger.error(
            "quilt refused event %s (%s for %s): %s %s; an admin must look at /admin",
            row.id,
            row.type,
            row.external_id,
            response.status,
            row.last_error,
        )
    else:
        row.next_attempt_at = now + backoff(row.attempts)


def deliver(
    session: Session,
    client: QuiltClient,
    weave_client: WeaveClient,
    *,
    limit: int,
    now: Callable[[], datetime] = _utcnow,
) -> DeliveryResult:
    """Send up to `limit` due rows, committing after each one. Returns what happened."""
    result = DeliveryResult()
    try:
        token = weave_client.quilt_token()
    except WeaveUnavailableError:
        logger.warning("quilt: no `quilt` token from Weave; the outbox waits for the next run", exc_info=True)
        result.stopped = "no_token"
        return result

    for _ in range(limit):
        current = now()
        row = next_row(session, current)
        if row is None:
            session.commit()
            break
        response = client.send_event(row.event_body(), token=token)
        outcome = classify(response)
        _record(row, response, outcome, current)
        session.commit()

        if outcome is Outcome.SENT:
            result.sent += 1
        elif outcome is Outcome.FAILED:
            result.failed += 1
        else:
            result.retried += 1
        if outcome is Outcome.RETRY_CONFIG:
            if response.status == 401:
                weave_client.invalidate_quilt_token()
            logger.error(
                "quilt answered %s (%s): check KRATER_QUILT_URL, the `quilt` scope on the Krater app in Weave, "
                "and the Ganymede patch's Weave client id in Quilt; the outbox waits",
                response.status,
                response.error,
            )
            result.stopped = f"http_{response.status}"
            break
    return result


_disabled_logged = False


def run(session: Session, weave_client: WeaveClient, settings: Settings) -> DeliveryResult | None:
    """The worker's entry point. Does nothing (and says so once per process) when `KRATER_QUILT_URL` is blank."""
    global _disabled_logged
    if not settings.quilt_url:
        if not _disabled_logged:
            logger.info("quilt: KRATER_QUILT_URL is blank; events stay in quilt_outbox until it is set")
            _disabled_logged = True
        return None
    client = QuiltClient(settings.quilt_url, timeout_seconds=settings.quilt_timeout_seconds)
    try:
        return deliver(session, client, weave_client, limit=settings.quilt_batch_size)
    finally:
        client.close()


__all__ = ["DeliveryResult", "Outcome", "backoff", "classify", "deliver", "next_row", "run"]
