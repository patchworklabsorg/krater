"""OSG's public accounting (GRACC): how much an OSPool project used, day by day.

GRACC is a read-only Elasticsearch endpoint anyone may query (https://osg-htc.org/gracc/user/direct/). Krater reads
only its summary index, `gracc.osg.summary`, which sums usage per project, site and day and carries no per-person
identity. Its raw index names each job's user, so Krater never reads it.

- **Which records.** `ResourceType: Payload` only: the user's jobs. `Batch` records are the pilot jobs (glideins)
  that ran them, and counting both would count the hours twice.
- **Days** are UTC, by each record's `EndTime`, so a job counts on the day it ended.
- **Fields.** `GPUHours`, `CoreHours` and `Njobs`. GRACC doesn't say which GPU a job used.
- **Lag.** OSG's accounting is about a day behind, and corrects recent days, so the import reads a window again.
- **Speed.** GRACC is slow (5 to 25 seconds a query, measured Nov 8, 2026), and the summary alias spans 34 shards.
  `pre_filter_shard_size=1` lets it skip the shards whose dates can't match (31 of them for a recent fortnight), which
  is quicker and lighter on OSG's server; the timeout is generous.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache

import httpx

from krater.config import get_settings

#: Skip the shards whose date range can't match before searching them (see "Speed" above).
SEARCH_PARAMS = {"pre_filter_shard_size": "1"}


class AccountingError(Exception):
    """OSG's accounting couldn't be read, or answered something Krater doesn't understand."""


@dataclass(frozen=True)
class DayUsage:
    """One UTC day of one OSPool project's usage."""

    day: date
    gpu_hours: Decimal
    core_hours: Decimal
    jobs: int


def _decimal(value: object) -> Decimal:
    # Elasticsearch sums come back as JSON floats. Through `str`, never as a float, and to 4 places, as stored.
    return Decimal(str(value or 0)).quantize(Decimal("0.0001"))


def usage_query(project_name: str, start: date, end: date) -> dict:
    """The search body: the project's user jobs that ended in `[start, end)`, summed per UTC day."""
    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"ProjectName": project_name}},
                    {"term": {"ResourceType": "Payload"}},
                    {"range": {"EndTime": {"gte": start.isoformat(), "lt": end.isoformat()}}},
                ]
            }
        },
        "aggs": {
            "days": {
                "date_histogram": {"field": "EndTime", "calendar_interval": "day", "time_zone": "UTC"},
                "aggs": {
                    "gpu": {"sum": {"field": "GPUHours"}},
                    "core": {"sum": {"field": "CoreHours"}},
                    "jobs": {"sum": {"field": "Njobs"}},
                },
            }
        },
    }


def parse_usage(body: dict) -> list[DayUsage]:
    """The days of a `usage_query` answer. Days with nothing at all are left out."""
    try:
        buckets = body["aggregations"]["days"]["buckets"]
        days = [
            DayUsage(
                day=datetime.fromtimestamp(int(bucket["key"]) / 1000, tz=UTC).date(),
                gpu_hours=_decimal(bucket["gpu"]["value"]),
                core_hours=_decimal(bucket["core"]["value"]),
                jobs=int(bucket["jobs"]["value"] or 0),
            )
            for bucket in buckets
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountingError(f"OSG's accounting answered in a shape Krater doesn't know: {exc!r}") from exc
    return [day for day in days if day.gpu_hours or day.core_hours or day.jobs]


class LiveAccounting:
    """Reads GRACC over HTTPS. `http_client` is injectable for tests."""

    def __init__(self, url: str, *, timeout_seconds: float, http_client: httpx.Client | None = None) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)

    def daily_usage(self, project_name: str, start: date, end: date) -> list[DayUsage]:
        """`project_name`'s usage on each day in `[start, end)`."""
        try:
            response = self._http.post(
                self._url,
                params=SEARCH_PARAMS,
                json=usage_query(project_name, start, end),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise AccountingError(f"could not reach OSG's accounting: {exc}") from exc
        if response.status_code >= 400:
            raise AccountingError(f"OSG's accounting answered HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise AccountingError("OSG's accounting didn't answer with JSON") from exc
        return parse_usage(body)


class FakeAccounting:
    """In-memory accounting for dev and tests: `set_day` records what OSG "reports"."""

    def __init__(self) -> None:
        self._days: dict[tuple[str, date], DayUsage] = {}
        self.error: AccountingError | None = None
        self.calls: list[tuple[str, date, date]] = []

    def set_day(
        self, project_name: str, day: date, *, gpu_hours: str = "0", core_hours: str = "0", jobs: int = 0
    ) -> None:
        self._days[(project_name, day)] = DayUsage(day, Decimal(gpu_hours), Decimal(core_hours), jobs)

    def daily_usage(self, project_name: str, start: date, end: date) -> list[DayUsage]:
        self.calls.append((project_name, start, end))
        if self.error is not None:
            raise self.error
        return sorted(
            (usage for (name, day), usage in self._days.items() if name == project_name and start <= day < end),
            key=lambda usage: usage.day,
        )


@lru_cache
def get_accounting_client() -> LiveAccounting | FakeAccounting:
    settings = get_settings()
    if settings.ospool_accounting_mode == "fake":
        return FakeAccounting()
    return LiveAccounting(settings.ospool_accounting_url, timeout_seconds=settings.ospool_accounting_timeout_seconds)
