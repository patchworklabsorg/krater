"""Krater's own Vast price history: what a sample holds, and how a run handles Vast failing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import Session

from krater.services import compute_settings, price_history
from krater.services.actor import Actor
from krater.vast import FakeVast, Offer, usd_to_microusd
from krater.vast.types import SEARCH_RESULT_CAP

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _offer(offer_id: int, min_bid: str, *, storage_cost: str = "0", num_gpus: int = 1) -> Offer:
    return Offer.from_json(
        {
            "id": offer_id,
            "gpu_name": "RTX 4090",
            "num_gpus": num_gpus,
            "dph_base": float(Decimal(min_bid) * 2),
            "dph_total": 0,
            "min_bid": float(min_bid),
            "storage_cost": float(storage_cost),
        }
    )


def test_summarize_counts_disk_and_takes_nearest_rank_percentiles() -> None:
    offers = [_offer(i, f"0.{i:02d}") for i in range(1, 21)]  # bids $0.01 .. $0.20

    summary = price_history.summarize(offers, "bid", disk_gb=32)

    assert summary.offer_count == 20
    assert summary.min_microusd == 10_000
    assert summary.p10_microusd == 20_000  # 2nd of 20
    assert summary.median_microusd == 100_000  # 10th of 20
    assert summary.truncated is False


def test_summarize_uses_dph_base_on_demand_and_adds_storage() -> None:
    summary = price_history.summarize([_offer(1, "0.10", storage_cost="0.73")], "ondemand", disk_gb=10)

    # dph_base $0.20 + $0.73/GB-month * 10 GB / 730 h = $0.21
    assert summary.min_microusd == usd_to_microusd(Decimal("0.21"))


def test_summarize_skips_multi_gpu_offers_and_flags_truncation() -> None:
    offers = [_offer(i, "0.10") for i in range(SEARCH_RESULT_CAP - 1)] + [_offer(9999, "0.01", num_gpus=2)]

    summary = price_history.summarize(offers, "bid", disk_gb=8)

    assert summary.offer_count == SEARCH_RESULT_CAP - 1
    assert summary.min_microusd == 100_000
    assert summary.truncated is True


def test_summarize_with_no_offers() -> None:
    summary = price_history.summarize([], "bid", disk_gb=8)

    assert summary == price_history.PriceSummary(0, False, None, None, None)


def test_record_writes_one_sample_per_model_and_rental_type(db_session: Session) -> None:
    fake = FakeVast()

    result = price_history.record(db_session, fake.client(), gpu_names=["RTX 3060", "H100 SXM"], disk_gb=32, now=NOW)

    assert result == price_history.RecordResult(written=4, failed=0, stopped_early=False)
    bid = price_history.series(db_session, gpu_name="RTX 3060", rental_type="bid", since=NOW - timedelta(hours=1))
    assert len(bid) == 1
    sample = bid[0]
    assert sample.offer_count == 3
    assert sample.disk_gb == 32
    # Cheapest RTX 3060 bid with 32 GB: offer 1001 ($0.050 + 0.20 * 32 / 730) vs 1002 ($0.045 + 0.80 * 32 / 730).
    assert sample.min_microusd == usd_to_microusd(Decimal("0.05") + Decimal("0.2") * 32 / 730)
    none = price_history.series(db_session, gpu_name="H100 SXM", rental_type="ondemand", since=NOW)
    assert none[0].offer_count == 0 and none[0].min_microusd is None
    # Every search asked for 1 GPU, the job disk and the global reliability floor.
    assert all(body["num_gpus"] == {"eq": 1} for body in fake.search_bodies)
    assert all(body["allocated_storage"] == 32 for body in fake.search_bodies)
    assert all(body["reliability"] == {"gte": 0.95} for body in fake.search_bodies)


def test_record_uses_the_admins_reliability_floor(db_session: Session, admin: Actor) -> None:
    values = {**compute_settings.global_policy(db_session).values(), "reliability_floor_bp": 9900}
    compute_settings.update_global(db_session, admin, values=values, reason="r")
    fake = FakeVast()

    price_history.record(db_session, fake.client(), gpu_names=["RTX 3060"], disk_gb=32, now=NOW)

    assert fake.search_bodies[0]["reliability"] == {"gte": 0.99}


def test_one_failing_search_is_skipped(db_session: Session) -> None:
    fake = FakeVast()
    fake.fail_next(502)

    result = price_history.record(db_session, fake.client(), gpu_names=["RTX 3060"], disk_gb=32, now=NOW)

    assert result == price_history.RecordResult(written=1, failed=1, stopped_early=False)


def test_a_rate_limit_stops_the_run(db_session: Session) -> None:
    fake = FakeVast()

    def rate_limit_after_two(request):
        if len(fake.search_bodies) >= 2:
            fake.fail_next(429)
        return fake.handle(request)

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(rate_limit_after_two))

    result = price_history.record(db_session, client, gpu_names=["RTX 3060", "RTX 4090"], disk_gb=32, now=NOW)

    assert result.stopped_early is True
    assert result.written == 2


def test_latest_returns_the_newest_sample_per_series(db_session: Session) -> None:
    fake = FakeVast()
    price_history.record(db_session, fake.client(), gpu_names=["RTX 4090"], disk_gb=32, now=NOW - timedelta(minutes=10))
    price_history.record(db_session, fake.client(), gpu_names=["RTX 4090"], disk_gb=32, now=NOW)

    newest = price_history.latest(db_session)

    assert [(s.gpu_name, s.rental_type, s.sampled_at) for s in newest] == [
        ("RTX 4090", "bid", NOW),
        ("RTX 4090", "ondemand", NOW),
    ]


@pytest.mark.parametrize("percent,expected", [(10, 1), (50, 5), (100, 10)])
def test_nearest_rank(percent: int, expected: int) -> None:
    assert price_history._nearest_rank(list(range(1, 11)), percent) == expected


def test_searches_are_paced(db_session: Session) -> None:
    fake = FakeVast()
    pauses: list[float] = []

    price_history.record(
        db_session,
        fake.client(),
        gpu_names=["RTX 3060", "RTX 4090"],
        disk_gb=32,
        now=NOW,
        pause_seconds=1.0,
        sleep=pauses.append,
    )

    assert len(fake.search_bodies) == 4
    assert pauses == [1.0, 1.0, 1.0]  # between searches, not before the first


def test_a_run_leaves_half_the_search_quota_to_the_scheduler(db_session: Session) -> None:
    # Price history used up Vast's daily quota of search rows on staging (2026-10-11), and then the scheduler
    # couldn't find an offer for anything.
    fake = FakeVast()
    fake.set_search_quota(100, remaining=52)

    result = price_history.record(db_session, fake.client(), gpu_names=["RTX 3060", "RTX 4090"], disk_gb=32, now=NOW)

    assert result.stopped_early is True
    assert result.written == 1
    assert len(fake.search_bodies) == 1
