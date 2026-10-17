"""How often the scheduler searches for a waiting job: back-off, the low-quota floor, and the quota's reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from krater.compute.search_pacing import SearchPacer
from krater.vast import SearchQuota

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 12, tzinfo=UTC)


def _minutes_searched(pacer: SearchPacer, minutes: int, *, quota: SearchQuota | None = None) -> list[int]:
    searched = []
    for minute in range(minutes):
        # Passes start a few seconds after the minute, never exactly on it.
        now = NOW + timedelta(minutes=minute, seconds=(minute * 7) % 5)
        if pacer.due("q", now=now, quota=quota):
            pacer.searched("q", now=now, rented=False)
            searched.append(minute)
    return searched


def test_a_search_that_finds_nothing_backs_off_to_every_15_minutes() -> None:
    assert _minutes_searched(SearchPacer(), 60) == [0, 1, 2, 4, 8, 16, 31, 46]


def test_a_rental_starts_the_back_off_over() -> None:
    pacer = SearchPacer()
    _minutes_searched(pacer, 20)
    pacer.searched("q", now=NOW + timedelta(minutes=20), rented=True)

    assert pacer.due("q", now=NOW + timedelta(minutes=21), quota=None)


def test_each_search_has_its_own_back_off() -> None:
    pacer = SearchPacer()
    _minutes_searched(pacer, 20)

    assert pacer.due("escalated", now=NOW + timedelta(minutes=20), quota=None)


def test_with_a_quarter_of_the_quota_left_searches_are_15_minutes_apart() -> None:
    low = SearchQuota(limit=20_000, remaining=4_000, reset_at=RESET)

    assert _minutes_searched(SearchPacer(), 31, quota=low) == [0, 15, 30]


def test_with_the_quota_used_up_nothing_is_searched_until_the_reset() -> None:
    pacer = SearchPacer()
    gone = SearchQuota(limit=20_000, remaining=0, reset_at=RESET)

    assert not pacer.due("q", now=NOW, quota=gone)
    assert pacer.due("q", now=RESET + timedelta(seconds=1), quota=gone)


def test_old_searches_are_forgotten() -> None:
    pacer = SearchPacer()
    pacer.searched("q", now=NOW, rented=False)
    pacer.forget_old(NOW + timedelta(days=2))

    assert pacer._searches == {}
