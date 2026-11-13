"""Live price checks for a job's shape (`krater.services.live_prices`): the scheduler's query and pricing, the cheapest
few rows, freshness, and the quota guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeJobKind, GpuPriceSample, LiveQuote
from krater.providers import ProviderRateLimitedError
from krater.services import compute_settings, live_prices
from krater.services.compute_jobs import JobSpec
from krater.vast import FakeVast

NOW = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)


def _spec(**overrides) -> JobSpec:
    fields = {
        "name": "train",
        "image": "ubuntu:24.04",
        "command": "true",
        "gpu_names": ("A100 SXM4",),
        "target_microusd": 1_000_000,
        "max_microusd": 1_000_000,
        "interruptible": False,
        "disk_gb": 32,
        **overrides,
    }
    return JobSpec(**fields)


def _shape(db_session: Session, **overrides) -> live_prices.Shape:
    return live_prices.shape_of(_spec(**overrides), compute_settings.global_policy(db_session))


def _market() -> FakeVast:
    fake = FakeVast()
    # On demand, by `dph_base`: 0.55, 0.78, 0.86. 4001 has no direct port, so a box can't have it.
    fake.add_offer(
        4001, 41, "A100 SXM4", dph_base=0.40, min_bid=0.30, storage_cost=0.1, gpu_ram_mb=40960, direct_port_count=0
    )
    fake.add_offer(4002, 42, "A100 SXM4", dph_base=0.55, min_bid=0.45, storage_cost=0.1, gpu_ram_mb=40960)
    fake.add_offer(4003, 43, "A100 SXM4", dph_base=0.78, min_bid=0.20, storage_cost=0.1, gpu_ram_mb=40960)
    fake.add_offer(4004, 44, "A100 SXM4", dph_base=0.86, min_bid=0.50, storage_cost=0.1, gpu_ram_mb=40960)
    fake.add_offer(5001, 51, "RTX 4090", dph_base=0.30, min_bid=0.15, storage_cost=0.1, gpu_ram_mb=24576)
    return fake


def _checked(db_session: Session, fake: FakeVast, shape: live_prices.Shape, *, now: datetime = NOW) -> LiveQuote:
    row, needs = live_prices.request(db_session, shape, now=now)
    assert needs
    return live_prices.check(db_session, fake.client(), row.key, now=now + timedelta(seconds=3))


def test_a_check_finds_the_cheapest_offer_the_job_could_rent(db_session: Session) -> None:
    fake = _market()

    row = _checked(db_session, fake, _shape(db_session))

    assert [offer["gpu"] for offer in row.offers] == ["A100 SXM4"] * 3
    assert 400_000 < row.cheapest_microusd < 410_000  # $0.40 an hour, and 32 GB of disk at $0.10 a GB-month
    assert row.error is None
    shown = live_prices.view(row)
    assert shown["status"] == "ready"
    assert shown["text"].startswith("Right now on Vast (checked 03:00:03 UTC) the cheapest on demand offer")
    assert "Krater rents what's cheapest when the job starts" in shown["text"]
    # Live checks never become price history: its charts stay one sample an hour.
    assert db_session.scalar(sa.select(sa.func.count()).select_from(GpuPriceSample)) == 0


def test_a_box_needs_a_direct_port(db_session: Session) -> None:
    row = _checked(db_session, _market(), _shape(db_session, kind=ComputeJobKind.BOX))

    assert row.cheapest_microusd > 550_000  # 4001 has no port for SSH


def test_an_interruptible_check_sorts_by_the_bid(db_session: Session, monkeypatch) -> None:
    """Vast sorts by `dph_total`; for a bid that isn't the price paid. 4003's bid is the lowest, its on-demand price
    the second highest: with a few rows sorted by `dph_total` it would be missed."""
    monkeypatch.setattr(live_prices, "QUOTE_ROWS", 1)
    fake = _market()

    row = _checked(db_session, fake, _shape(db_session, interruptible=True))

    margin = compute_settings.global_policy(db_session).bid_margin_percent
    # 4003: $0.20 plus the margin plus 32 GB of disk; by `dph_total` the one row would be 4001, at $0.30 plus.
    assert row.cheapest_microusd == 200_000 * (100 + margin) // 100 + 4_384
    shown = live_prices.view(row)
    assert "cheapest interruptible offer" in shown["text"]


def test_several_models_say_which_and_that_krater_weighs_performance(db_session: Session) -> None:
    row = _checked(db_session, _market(), _shape(db_session, gpu_names=("A100 SXM4", "RTX 4090")))

    text = live_prices.view(row)["text"]
    assert "(RTX 4090)" in text and "best performance per dollar" in text


def test_a_fresh_check_answers_again_and_a_pending_one_isnt_asked_twice(db_session: Session) -> None:
    fake = _market()
    shape = _shape(db_session)
    row, needs = live_prices.request(db_session, shape, now=NOW)
    assert needs and live_prices.view(row)["status"] == "checking"
    assert live_prices.request(db_session, shape, now=NOW + timedelta(seconds=20)) == (row, False)  # pending
    live_prices.check(db_session, fake.client(), row.key, now=NOW + timedelta(seconds=30))

    assert live_prices.request(db_session, shape, now=NOW + timedelta(minutes=1)) == (row, False)  # fresh
    again, needs = live_prices.request(db_session, shape, now=NOW + timedelta(minutes=3))
    assert again is row and needs and live_prices.view(row)["status"] == "checking"


def test_a_check_leaves_half_of_vasts_quota_for_the_scheduler(db_session: Session) -> None:
    fake = _market()
    fake.set_search_quota(20_000, remaining=9_000, reset_at=int((NOW + timedelta(hours=21)).timestamp()))
    client = fake.client()
    client.search_offers(
        live_prices.offers.shape_query(
            rental_type="ondemand",
            gpu_names=("RTX 4090",),
            min_gpu_ram_gb=None,
            num_gpus=1,
            disk_gb=32,
            box=False,
            reliability_floor=0.9,
            transfer_cap_microusd_per_gb=None,
            limit=1,
        )
    )  # the client learns the quota
    row, _ = live_prices.request(db_session, _shape(db_session), now=NOW)

    live_prices.check(db_session, client, row.key, now=NOW)

    assert row.cheapest_microusd is None and row.offers == []
    assert live_prices.view(row) == {
        "status": "unavailable",
        "text": "Krater is saving today's Vast searches for starting jobs, so it can't check live prices now.",
        "checked_at": NOW.isoformat(),
    }


def test_krater_s_own_daily_cap_holds_without_vasts_numbers(db_session: Session, monkeypatch) -> None:
    """Production hadn't seen Vast's quota headers yet (2026-11-13): Krater counts its own searches too."""
    monkeypatch.setitem(live_prices.DAILY_SEARCHES, live_prices.PURPOSE_QUOTE, 2)
    fake = _market()
    for minutes, gpu in ((0, ("A100 SXM4",)), (1, ("RTX 4090",))):
        assert (
            _checked(db_session, fake, _shape(db_session, gpu_names=gpu), now=NOW + timedelta(minutes=minutes)).error
            is None
        )

    third = _checked(db_session, fake, _shape(db_session, num_gpus=2), now=NOW + timedelta(minutes=2))

    assert third.error == "Krater has used today's live price checks. They're back at midnight UTC."
    tomorrow = _checked(db_session, fake, _shape(db_session, num_gpus=4), now=NOW + timedelta(days=1))
    assert tomorrow.error is None


def test_nothing_that_fits_says_so(db_session: Session) -> None:
    row = _checked(db_session, _market(), _shape(db_session, gpu_names=("H200",)))

    assert row.cheapest_microusd is None
    assert live_prices.view(row)["text"] == "Right now on Vast (checked 03:00:03 UTC) no offer fits this job."


# --- the form's "Cheapest now" column ---------------------------------------------------------------------------------

BOTH = ("bid", "ondemand")


def _refresh(db_session: Session, client, *, at: datetime = NOW) -> list[int]:
    commits: list[int] = []
    live_prices.check_table(
        db_session,
        client,
        disk_gb=32,
        clock=lambda: at,
        on_row=lambda: commits.append(1),
        pause_seconds=0,
    )
    return commits


def test_opening_the_form_refreshes_the_table_once_and_fills_it_in(db_session: Session) -> None:
    fake = _market()
    assert live_prices.request_table(db_session, ["A100 SXM4", "RTX 4090"], BOTH, now=NOW)
    shown = live_prices.view_table(db_session)
    assert shown["done"] is False and shown["rows"]["A100 SXM4|bid"]["status"] == "checking"
    # Another member opening the form meanwhile doesn't ask again.
    assert not live_prices.request_table(db_session, ["A100 SXM4", "RTX 4090"], BOTH, now=NOW + timedelta(seconds=30))

    commits = _refresh(db_session, fake.client(), at=NOW + timedelta(seconds=5))

    assert len(commits) == 4  # one per row, so the page fills in as it goes
    shown = live_prices.view_table(db_session)
    assert shown["done"] is True and shown["note"] is None
    # The hourly table's measure: GPU price plus 32 GB of disk, no bid margin, 1-GPU offers, no direct port needed.
    assert shown["rows"]["A100 SXM4|ondemand"] == {"status": "ready", "text": "$0.4044/h", "at": "03:00:05 UTC"}
    assert shown["rows"]["A100 SXM4|bid"]["text"] == "$0.2044/h"
    assert shown["rows"]["RTX 4090|bid"]["text"] == "$0.1544/h"
    assert db_session.scalar(sa.select(sa.func.count()).select_from(GpuPriceSample)) == 0

    assert not live_prices.request_table(db_session, ["A100 SXM4", "RTX 4090"], BOTH, now=NOW + timedelta(minutes=2))
    assert live_prices.request_table(db_session, ["A100 SXM4", "RTX 4090"], BOTH, now=NOW + timedelta(minutes=6))


def test_the_table_has_its_own_daily_cap(db_session: Session, monkeypatch) -> None:
    monkeypatch.setitem(live_prices.DAILY_SEARCHES, live_prices.PURPOSE_TABLE, 1)
    live_prices.request_table(db_session, ["A100 SXM4"], BOTH, now=NOW)

    _refresh(db_session, _market().client())

    shown = live_prices.view_table(db_session)
    assert shown["rows"]["A100 SXM4|bid"]["status"] == "ready"
    assert shown["rows"]["A100 SXM4|ondemand"]["status"] == "unavailable"
    assert shown["note"] == "Krater has used today's live price checks. They're back at midnight UTC."
    assert shown["done"] is True
    # A quote has its own count: the table using up its share leaves the estimate's.
    assert _checked(db_session, _market(), _shape(db_session)).error is None


def test_a_rate_limit_stops_the_refresh(db_session: Session) -> None:
    class Limited:
        search_quota = None
        calls = 0

        def search_offers(self, query):
            self.calls += 1
            raise ProviderRateLimitedError("slow down")

    client = Limited()
    live_prices.request_table(db_session, ["A100 SXM4", "RTX 4090"], BOTH, now=NOW)

    _refresh(db_session, client)

    assert client.calls == 1
    shown = live_prices.view_table(db_session)
    assert {row["status"] for row in shown["rows"].values()} == {"unavailable"}
    assert shown["note"] == "Vast asked Krater to slow down. Try again in a few minutes."
