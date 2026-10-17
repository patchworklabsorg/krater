"""How often the scheduler searches Vast for a waiting job's offers.

Vast counts every search against a daily quota of rows (20,000 a day for the staging account; docs/compute/vast-api.md).
Once it's used up, no search works until 00:00 UTC, and Krater can't start anything. "Run when cheap" means jobs often
wait for hours, so a search a minute per waiting job would use the quota up long before then (staging, 2026-10-11).

So each search (the query plus the price it must meet) backs off while it keeps finding nothing to rent: a minute, a
minute, then 2, 4, 8 and at most 15 minutes apart. A search that rents something starts over. When less than a
quarter of the quota is left, searches are at least 15 minutes apart, and with none left, none are made until the
reset. Escalating a job (a raised bid, or on demand) changes its search, so that search runs at once.

The memory lives in the worker process (`SEARCH_PACER` in `krater.worker.app`): a restart searches everything once.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from krater.vast import SearchQuota

#: Minutes between searches after 0, 1, 2, ... searches in a row that rented nothing.
BACKOFF_MINUTES = (1, 1, 2, 4, 8, 15)
#: Below this share of the quota left, searches are at least `LOW_QUOTA_MINUTES` apart.
LOW_QUOTA_FRACTION = 0.25
LOW_QUOTA_MINUTES = 15
#: Passes start on the minute but not exactly: a search due "a minute later" must not slip to the pass after.
PASS_JITTER = timedelta(seconds=10)
#: Searches not repeated for this long are forgotten.
FORGET_AFTER = timedelta(days=1)


class SearchPacer:
    def __init__(self) -> None:
        # search key -> (last searched at, searches in a row that rented nothing)
        self._searches: dict[str, tuple[datetime, int]] = {}

    def due(self, key: str, *, now: datetime, quota: SearchQuota | None) -> bool:
        if quota is not None and quota.exhausted(now):
            return False
        last = self._searches.get(key)
        if last is None:
            return True
        at, misses = last
        wait = BACKOFF_MINUTES[min(misses, len(BACKOFF_MINUTES) - 1)]
        if quota is not None and quota.fraction_left(now) < LOW_QUOTA_FRACTION:
            wait = max(wait, LOW_QUOTA_MINUTES)
        return now - at >= timedelta(minutes=wait) - PASS_JITTER

    def searched(self, key: str, *, now: datetime, rented: bool) -> None:
        misses = 0 if rented else self._searches.get(key, (now, -1))[1] + 1
        self._searches[key] = (now, misses)

    def forget_old(self, now: datetime) -> None:
        self._searches = {k: v for k, v in self._searches.items() if now - v[0] < FORGET_AFTER}
