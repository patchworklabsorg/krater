"""Saving Vast's search quota for admins, and the once-a-day alerts when it runs low or out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from krater.compute import search_quota
from krater.models import AuditEvent
from krater.vast import SearchQuota

NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 12, tzinfo=UTC)


def _alerts(db_session) -> list[str]:
    stmt = sa.select(AuditEvent.action).where(AuditEvent.action.like("compute_search_quota_%"))
    return sorted(db_session.scalars(stmt))


def test_nothing_is_saved_before_the_first_search(db_session) -> None:
    search_quota.record(db_session, None, now=NOW)

    assert search_quota.latest(db_session) is None


def test_the_latest_quota_is_saved(db_session) -> None:
    search_quota.record(db_session, SearchQuota(limit=20_000, remaining=15_000, reset_at=RESET), now=NOW)
    search_quota.record(db_session, SearchQuota(limit=20_000, remaining=14_000, reset_at=RESET), now=NOW + timedelta(1))

    row = search_quota.latest(db_session)
    assert (row.limit_rows, row.remaining_rows, row.reset_at) == (20_000, 14_000, RESET)
    assert _alerts(db_session) == []


def test_low_and_used_up_each_alert_once_per_day(db_session) -> None:
    for remaining in (4_000, 3_000, 0, 0):
        search_quota.record(db_session, SearchQuota(limit=20_000, remaining=remaining, reset_at=RESET), now=NOW)
    assert _alerts(db_session) == ["compute_search_quota_exhausted", "compute_search_quota_low"]

    tomorrow = RESET + timedelta(days=1)
    later = RESET + timedelta(hours=20)
    search_quota.record(db_session, SearchQuota(limit=20_000, remaining=0, reset_at=tomorrow), now=later)
    assert _alerts(db_session).count("compute_search_quota_exhausted") == 2
