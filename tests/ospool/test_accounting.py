"""OSG's public accounting client (`krater.ospool.accounting`): the query it sends and how it reads the answer."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from krater.ospool.accounting import AccountingError, DayUsage, LiveAccounting

URL = "https://gracc.example/q/gracc.osg.summary/_search"


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _bucket(day: date, gpu: float, core: float, jobs: float) -> dict:
    return {"key": _ms(day), "gpu": {"value": gpu}, "core": {"value": core}, "jobs": {"value": jobs}}


def _client(handler) -> LiveAccounting:
    return LiveAccounting(URL, timeout_seconds=5, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_it_asks_for_the_projects_user_jobs_per_utc_day() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert str(request.url) == f"{URL}?pre_filter_shard_size=1"
        body = {"aggregations": {"days": {"buckets": [_bucket(date(2026, 11, 3), 10.123456, 84.0, 12.0)]}}}
        return httpx.Response(200, json=body)

    days = _client(handler).daily_usage("PatchworkLabs_Ganymede01", date(2026, 11, 1), date(2026, 11, 5))

    assert days == [DayUsage(date(2026, 11, 3), Decimal("10.1235"), Decimal("84.0000"), 12)]
    query = seen[0]
    assert query["size"] == 0
    filters = query["query"]["bool"]["filter"]
    assert {"term": {"ProjectName": "PatchworkLabs_Ganymede01"}} in filters
    # The user's jobs only: the pilots that ran them would count the hours twice.
    assert {"term": {"ResourceType": "Payload"}} in filters
    assert {"range": {"EndTime": {"gte": "2026-11-01", "lt": "2026-11-05"}}} in filters
    histogram = query["aggs"]["days"]["date_histogram"]
    assert histogram == {"field": "EndTime", "calendar_interval": "day", "time_zone": "UTC"}


def test_empty_days_are_left_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        buckets = [_bucket(date(2026, 11, 3), 0, 0, 0), _bucket(date(2026, 11, 4), 0, 2.5, 1)]
        return httpx.Response(200, json={"aggregations": {"days": {"buckets": buckets}}})

    days = _client(handler).daily_usage("X1", date(2026, 11, 1), date(2026, 11, 5))

    assert [day.day for day in days] == [date(2026, 11, 4)]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="down"),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"hits": {}}),
    ],
)
def test_a_bad_answer_is_an_accounting_error(response) -> None:
    with pytest.raises(AccountingError):
        _client(lambda request: response).daily_usage("X1", date(2026, 11, 1), date(2026, 11, 5))


def test_an_unreachable_server_is_an_accounting_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AccountingError, match="could not reach"):
        _client(handler).daily_usage("X1", date(2026, 11, 1), date(2026, 11, 5))
