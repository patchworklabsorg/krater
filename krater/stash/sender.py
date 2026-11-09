"""The sender: asks Stash to archive the `stash_archives` rows the services wrote, oldest first, one row per
transaction, and then refreshes the status of the archives Stash is still working on.

Rules (see `docs/stash-integration.md`, "Delivery"):

- Each row is taken with `SELECT ... FOR UPDATE SKIP LOCKED`, so two workers never ask for the same URL twice.
- 201 and 200 mean archived: the row keeps Stash's archive id and page URL. 5xx (including 503
  `weave_unavailable`) and network errors are retried with backoff. 429 waits `Retry-After` and counts no attempt,
  and stops the run: the limit is per token, so every later request would get it too. 401 and 403 are retried
  too, but they are a configuration error: the run stops and logs it. 422 and 413 fail for good: the URL can't be
  archived.
- A member's request never waits on Stash, and a submission never fails because Stash is down: the rows wait.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import StashArchive, StashArchiveState
from krater.models.stash_archive import FINAL_ARCHIVE_STATUSES
from krater.stash.client import StashClient, StashResponse
from krater.weave import WeaveClient, WeaveUnavailableError

logger = logging.getLogger(__name__)

#: The first retry waits this long; each later one waits twice as long, up to `MAX_BACKOFF`.
BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(hours=1)

#: What a 429 without a usable `Retry-After` waits.
DEFAULT_RETRY_AFTER = timedelta(seconds=60)

#: An archive is asked about again only after this long without a change: Stash needs at least this long for a
#: capture, and a run never re-reads what it just created.
REFRESH_MIN_AGE = timedelta(seconds=60)

#: The `reference.patch` Krater sends: Stash shows it on the archive page, and Quilt knows Krater by this name too.
PATCH = "ganymede"


class Outcome(Enum):
    ARCHIVED = "archived"
    RETRY = "retry"
    RETRY_CONFIG = "retry_config"  # 401/403: retry later, but stop this run, and tell an operator
    RATE_LIMITED = "rate_limited"  # 429: wait `Retry-After`, no attempt counted, stop this run
    FAILED = "failed"


def classify(response: StashResponse) -> Outcome:
    """What one answer from Stash means for the row."""
    status = response.status
    if status in (200, 201):
        # A 2xx without an archive body is not the contract: treat it as a transient fault and ask again.
        return Outcome.ARCHIVED if response.archive is not None else Outcome.RETRY
    if status is None or status >= 500:
        return Outcome.RETRY
    if status == 429:
        return Outcome.RATE_LIMITED
    if status in (401, 403):
        return Outcome.RETRY_CONFIG
    if status in (413, 422):
        return Outcome.FAILED
    # Anything else (a 404 from a wrong KRATER_STASH_URL, a 400 from a proxy) is not the URL's fault.
    return Outcome.RETRY_CONFIG


def backoff(attempts: int) -> timedelta:
    """How long to wait after the `attempts`-th failed attempt."""
    return min(BASE_BACKOFF * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)


@dataclass
class DeliveryResult:
    archived: int = 0
    retried: int = 0
    failed: int = 0
    refreshed: int = 0
    stopped: str | None = None  # why the run stopped early, if it did


def _utcnow() -> datetime:
    return datetime.now(UTC)


def archive_request(row: StashArchive, *, base_url: str) -> dict:
    """The JSON body for `POST /api/v1/archives`: the URL, a note for the archive page, and the reference
    that makes the request idempotent (`submission` is the Krater `Project.id`, as Quilt's `external_id`).
    The note names the submitter by Patchwork ID only (docs/weave-integration.md "Member data")."""
    project = row.project
    return {
        "url": row.url,
        "note": f"Ganymede submission {project.title!r} ({row.source}), by {project.submitter.weave_sub}."[:2000],
        "reference": {
            "patch": PATCH,
            "submission": str(project.id),
            "revision": str(row.revision_id) if row.revision_id is not None else "",
            "source": row.source,
            "project_url": f"{base_url.rstrip('/')}/projects/{project.id}",
        },
    }


def next_row(session: Session, now: datetime) -> StashArchive | None:
    """Lock and return the oldest pending row that may be sent now, or `None`."""
    stmt = (
        sa.select(StashArchive)
        .where(
            StashArchive.state == StashArchiveState.PENDING,
            sa.or_(StashArchive.next_attempt_at.is_(None), StashArchive.next_attempt_at <= now),
        )
        .order_by(StashArchive.seq)
        .limit(1)
        .with_for_update(skip_locked=True, of=StashArchive)
    )
    return session.scalars(stmt).first()


def next_unfinished(session: Session, exclude: set, *, before: datetime) -> StashArchive | None:
    """Lock and return an archived row whose archive Stash hasn't finished and that nothing touched since
    `before`, least recently updated first."""
    stmt = (
        sa.select(StashArchive)
        .where(
            StashArchive.state == StashArchiveState.ARCHIVED,
            sa.or_(StashArchive.archive_status.is_(None), StashArchive.archive_status.not_in(FINAL_ARCHIVE_STATUSES)),
            StashArchive.updated_at <= before,
            StashArchive.id.not_in(exclude) if exclude else sa.true(),
        )
        .order_by(StashArchive.updated_at, StashArchive.seq)
        .limit(1)
        .with_for_update(skip_locked=True, of=StashArchive)
    )
    return session.scalars(stmt).first()


def _apply_archive(row: StashArchive, archive: dict) -> None:
    """Copy what Stash says about the archive onto the row."""
    row.archive_id = str(archive.get("id") or row.archive_id or "")[:64]
    page_url = archive.get("page_url")
    if isinstance(page_url, str) and page_url:
        row.page_url = page_url[:2048]
    status = archive.get("status")
    row.archive_status = status[:32] if isinstance(status, str) else row.archive_status
    # Stash's `problem`: set while a capture has failed, null otherwise (a rerun that works clears it).
    problem = archive.get("problem")
    if isinstance(problem, dict):
        code, hint, message = problem.get("code"), problem.get("hint"), problem.get("message")
        row.problem_code = code[:32] if isinstance(code, str) and code else None
        row.problem_hint = hint[:1000] if isinstance(hint, str) and hint else None
        row.problem_message = message[:2000] if isinstance(message, str) and message else None
    elif "problem" in archive:
        row.problem_code = row.problem_hint = row.problem_message = None
    completed_at = archive.get("completed_at")
    if isinstance(completed_at, str) and completed_at:
        with contextlib.suppress(ValueError):
            row.archive_completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))


def _retry_after(response: StashResponse) -> timedelta:
    if response.retry_after is None:
        return DEFAULT_RETRY_AFTER
    return timedelta(seconds=response.retry_after)


def _record(row: StashArchive, response: StashResponse, outcome: Outcome, now: datetime) -> None:
    row.last_status = response.status
    row.updated_at = now  # explicit, so the refresh's "untouched since" check runs on the run's clock
    if row.first_attempted_at is None:
        row.first_attempted_at = now
    if outcome is Outcome.RATE_LIMITED:
        # Not the row's fault: wait what Stash asks, and keep the backoff where it was.
        row.last_error = response.error or "rate_limited"
        row.next_attempt_at = now + _retry_after(response)
        return
    row.attempts += 1
    if outcome is Outcome.ARCHIVED:
        assert response.archive is not None
        row.state = StashArchiveState.ARCHIVED
        row.requested_at = now
        row.next_attempt_at = None
        row.last_error = None
        _apply_archive(row, response.archive)
        return
    row.last_error = response.error or (response.body[:500] if response.body else None)
    if outcome is Outcome.FAILED:
        row.state = StashArchiveState.FAILED
        row.next_attempt_at = None
        logger.warning(
            "stash refused to archive %s for project %s: %s %s",
            row.url,
            row.project_id,
            response.status,
            row.last_error,
        )
    else:
        row.next_attempt_at = now + backoff(row.attempts)


def _is_config_error(status: int | None) -> bool:
    """A 4xx other than 404 (the archive is gone), 429 (slow down) and 422 (the URL) points at the setup."""
    return status is not None and 400 <= status < 500 and status not in (404, 422, 429)


def _config_error(response: StashResponse) -> None:
    logger.error(
        "stash answered %s (%s): check KRATER_STASH_URL and the `stash` scope on the Krater app in Weave; "
        "the archive requests wait",
        response.status,
        response.error,
    )


def deliver(
    session: Session,
    client: StashClient,
    weave_client: WeaveClient,
    *,
    limit: int,
    base_url: str,
    now: Callable[[], datetime] = _utcnow,
) -> DeliveryResult:
    """Ask Stash to archive up to `limit` due rows, committing after each one, then refresh up to `limit`
    unfinished archives. Returns what happened."""
    result = DeliveryResult()
    try:
        token = weave_client.stash_token()
    except WeaveUnavailableError:
        logger.warning("stash: no `stash` token from Weave; the archive requests wait for the next run", exc_info=True)
        result.stopped = "no_token"
        return result

    for _ in range(limit):
        current = now()
        row = next_row(session, current)
        if row is None:
            session.commit()
            break
        response = client.create_archive(token=token, **archive_request(row, base_url=base_url))
        outcome = classify(response)
        _record(row, response, outcome, current)
        session.commit()

        if outcome is Outcome.ARCHIVED:
            result.archived += 1
        elif outcome is Outcome.FAILED:
            result.failed += 1
        else:
            result.retried += 1
        if outcome is Outcome.RATE_LIMITED:
            # The limit is per token, so the rest of this run would hit it too. The next run carries on.
            logger.info("stash: rate limited; the next run carries on after %s", row.next_attempt_at)
            result.stopped = "rate_limited"
            return result
        if outcome is Outcome.RETRY_CONFIG:
            if response.status == 401:
                weave_client.invalidate_stash_token()
            _config_error(response)
            result.stopped = f"http_{response.status}"
            return result

    _refresh(session, client, token, result, limit=limit, weave_client=weave_client, now=now)
    return result


def _refresh(
    session: Session,
    client: StashClient,
    token: str,
    result: DeliveryResult,
    *,
    limit: int,
    weave_client: WeaveClient,
    now: Callable[[], datetime],
) -> None:
    """Fetch the current status of archives Stash is still working on. A row that doesn't change (Stash still
    running, or a transient error) is skipped for the rest of this run, so one run never loops on it."""
    seen: set = set()
    for _ in range(limit):
        current = now()
        row = next_unfinished(session, seen, before=current - REFRESH_MIN_AGE)
        if row is None:
            session.commit()
            break
        seen.add(row.id)
        assert row.archive_id is not None
        response = client.get_archive(row.archive_id, token=token)
        if response.status in (200, 201) and response.archive is not None:
            before = row.archive_status
            _apply_archive(row, response.archive)
            row.last_status = response.status
            row.last_error = None
            if row.archive_status != before:
                result.refreshed += 1
        elif response.status == 404:
            # An admin deleted the archive in Stash. Stop asking; the page says the copy is gone.
            row.archive_status = "missing"
            row.last_status = response.status
            row.last_error = response.error
            result.refreshed += 1
        elif response.status == 429:
            # Leave the row as it was (not even `updated_at`), and let the next run read the rest.
            session.rollback()
            logger.info("stash: rate limited while reading archives; the next run carries on")
            result.stopped = "rate_limited"
            return
        elif _is_config_error(response.status):
            row.last_status = response.status
            row.last_error = response.error
            session.commit()
            if response.status == 401:
                weave_client.invalidate_stash_token()
            _config_error(response)
            result.stopped = f"http_{response.status}"
            return
        else:
            row.last_status = response.status
            row.last_error = response.error or (response.body[:500] if response.body else None)
        # `updated_at` moves on every look, so the next run takes the other rows first, and this one waits
        # `REFRESH_MIN_AGE`.
        row.updated_at = current
        session.commit()


_disabled_logged = False


def run(session: Session, weave_client: WeaveClient, settings: Settings) -> DeliveryResult | None:
    """The worker's entry point. Does nothing (and says so once per process) when `KRATER_STASH_URL` is blank."""
    global _disabled_logged
    if not settings.stash_url:
        if not _disabled_logged:
            logger.info("stash: KRATER_STASH_URL is blank; submission URLs stay in stash_archives until it is set")
            _disabled_logged = True
        return None
    client = StashClient(settings.stash_url, timeout_seconds=settings.stash_timeout_seconds)
    try:
        return deliver(session, client, weave_client, limit=settings.stash_batch_size, base_url=settings.base_url)
    finally:
        client.close()


__all__ = [
    "DeliveryResult",
    "Outcome",
    "archive_request",
    "backoff",
    "classify",
    "deliver",
    "next_row",
    "next_unfinished",
    "run",
]
