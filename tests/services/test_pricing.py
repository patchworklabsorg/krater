"""Tests for `krater.services.pricing`: aggregation, atomic replace, fail-soft, and the estimator.

`tests/fixtures/pricing/vast_vms_sample.csv` is a real capture of SkyPilot's public Vast catalog (see
docs/dev/pricing.md), parsed the same way `LiveCatalog.list_gpu_prices` does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from krater.models import GpuPrice
from krater.pricing.catalog import CatalogError, FakeCatalog, GpuOffer
from krater.pricing.catalog import parse_vast_catalog_csv as _parse_vast_catalog_csv
from krater.services import pricing
from krater.services.errors import ValidationFailed

FIXTURE_CSV = Path(__file__).resolve().parent.parent / "fixtures" / "pricing" / "vast_vms_sample.csv"


class _BrokenCatalog:
    """A catalog whose `list_gpu_prices` always fails, for fail-soft tests."""

    def list_gpu_prices(self) -> list[GpuOffer]:
        raise CatalogError("the catalog is down")


def _seed_gpu_price(session: Session, *, name: str = "A100", count: int = 1) -> GpuPrice:
    price = GpuPrice(
        accelerator_name=name,
        accelerator_count=count,
        vram_gib=40.0,
        vcpus_typical=32.0,
        memory_gib_typical=128.0,
        on_demand_min_cents=100,
        on_demand_median_cents=120,
        spot_min_cents=35,
        offer_count=3,
        refreshed_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    session.add(price)
    session.flush()
    return price


# --------------------------------------------------------------------------------------------------
# Parsing the real captured CSV
# --------------------------------------------------------------------------------------------------


def test_parses_the_real_captured_catalog_csv() -> None:
    offers = _parse_vast_catalog_csv(FIXTURE_CSV.read_text())

    assert len(offers) == 64  # 65 lines minus the header
    assert all(isinstance(offer, GpuOffer) for offer in offers)
    names = {offer.accelerator_name for offer in offers}
    assert "A100" in names
    assert "H100" in names

    a100_single = next(o for o in offers if o.accelerator_name == "A100" and o.accelerator_count == 1)
    assert a100_single.price_dollars == pytest.approx(0.93)
    assert a100_single.device_memory_gib == pytest.approx(81920 / 1024, rel=1e-3)
    # The real capture happened to have no spot prices quoted anywhere (SpotPrice is 0.00 throughout) --
    # aggregate_offers treats that as "not quoted", not a real $0/hr offer; see the aggregation test below.
    assert all(offer.spot_price_dollars == 0.0 for offer in offers)


# --------------------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------------------


def test_aggregate_offers_groups_by_name_and_count_from_the_real_csv() -> None:
    offers = _parse_vast_catalog_csv(FIXTURE_CSV.read_text())

    aggregates = pricing.aggregate_offers(offers)
    by_key = {(a.accelerator_name, a.accelerator_count): a for a in aggregates}

    a100_1 = by_key[("A100", 1)]
    # Two A100x1 rows in the fixture, both priced at $0.93.
    assert a100_1.offer_count == 2
    assert a100_1.on_demand_min_cents == 93
    assert a100_1.on_demand_median_cents == 93
    # No positive spot price anywhere in this capture.
    assert a100_1.spot_min_cents is None
    # VRAM differs between the two A100x1 rows (81920 MiB vs 40960 MiB) -- median of the two.
    assert a100_1.vram_gib == pytest.approx(statistics_median_of([81920 / 1024, 40960 / 1024]), rel=1e-3)

    h200_8 = by_key[("H200", 8)]
    assert h200_8.offer_count == 2
    assert h200_8.on_demand_min_cents == 2071


def statistics_median_of(values: list[float]) -> float:
    import statistics

    return statistics.median(values)


def test_aggregate_offers_spot_min_ignores_unquoted_zero_prices() -> None:
    offers = [
        GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.00, 0.0, "US, NA"),  # not quoted
        GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.10, 0.30, "EU, DE"),
        GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.20, 0.25, "AS, KR"),
    ]

    (aggregate,) = pricing.aggregate_offers(offers)

    assert aggregate.spot_min_cents == 25  # min of the two *quoted* spot prices, zero excluded
    assert aggregate.on_demand_min_cents == 100
    assert aggregate.on_demand_median_cents == 110


def test_aggregate_offers_drops_non_positive_on_demand_prices() -> None:
    offers = [
        GpuOffer("A100", 1, 32.0, 128.0, 40.0, 0.0, 0.0, "US, NA"),
        GpuOffer("A100", 1, 32.0, 128.0, 40.0, -1.0, 0.0, "US, NA"),
    ]

    assert pricing.aggregate_offers(offers) == []


def test_aggregate_offers_handles_missing_vcpus_memory_vram() -> None:
    offers = [GpuOffer("V100", 1, None, None, None, 0.5, 0.0, "US, NA")]

    (aggregate,) = pricing.aggregate_offers(offers)

    assert aggregate.vram_gib is None
    assert aggregate.vcpus_typical is None
    assert aggregate.memory_gib_typical is None


# --------------------------------------------------------------------------------------------------
# refresh_prices: atomic replace + fail soft
# --------------------------------------------------------------------------------------------------


def test_refresh_prices_replaces_rows_and_returns_the_group_count(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="STALE-GPU", count=1)

    client = FakeCatalog()
    count = pricing.refresh_prices(db_session, client)
    db_session.commit()

    assert count == len({(o.accelerator_name, o.accelerator_count) for o in client.list_gpu_prices()})
    names = {row.accelerator_name for row in pricing.list_prices(db_session)}
    assert "STALE-GPU" not in names
    assert "A100" in names


def test_refresh_prices_fails_soft_and_keeps_old_rows_on_a_source_error(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)
    db_session.commit()

    with pytest.raises(CatalogError):
        pricing.refresh_prices(db_session, _BrokenCatalog())
    db_session.rollback()

    rows = pricing.list_prices(db_session)
    assert len(rows) == 1
    assert rows[0].accelerator_name == "A100"
    assert rows[0].on_demand_min_cents == 100


def test_refresh_prices_is_atomic_within_the_caller_transaction(db_session: Session) -> None:
    """A crash *after* `refresh_prices` flushes but before the caller commits rolls back to the old
    rows -- there's no window where the table is empty and durable."""
    _seed_gpu_price(db_session, name="A100", count=1)
    db_session.commit()

    pricing.refresh_prices(db_session, FakeCatalog())
    # Simulate the caller's transaction never committing (e.g. an exception elsewhere in the request).
    db_session.rollback()

    rows = pricing.list_prices(db_session)
    assert len(rows) == 1
    assert rows[0].accelerator_name == "A100"


# --------------------------------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------------------------------


def test_list_prices_sorts_by_price(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="B-GPU", count=1)
    cheap = _seed_gpu_price(db_session, name="A-GPU", count=1)
    cheap.on_demand_median_cents = 10
    db_session.flush()

    rows = pricing.list_prices(db_session, sort="price")
    assert [row.accelerator_name for row in rows] == ["A-GPU", "B-GPU"]


def test_list_prices_filters_by_accelerator_name(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)
    _seed_gpu_price(db_session, name="H100", count=1)

    rows = pricing.list_prices(db_session, accelerator_name="H100")
    assert [row.accelerator_name for row in rows] == ["H100"]


def test_last_refreshed_at_is_none_before_first_refresh(db_session: Session) -> None:
    assert pricing.last_refreshed_at(db_session) is None


def test_gpu_key_round_trips() -> None:
    assert pricing.parse_gpu_key(pricing.gpu_key("A100", 2)) == ("A100", 2)


def test_parse_gpu_key_rejects_malformed_input() -> None:
    assert pricing.parse_gpu_key("not-a-key") is None
    assert pricing.parse_gpu_key("A100:") is None
    assert pricing.parse_gpu_key("A100:abc") is None


# --------------------------------------------------------------------------------------------------
# Estimator
# --------------------------------------------------------------------------------------------------


def test_estimate_cost_on_demand_uses_the_median_price(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)

    result = pricing.estimate_cost(
        db_session, accelerator_name="A100", accelerator_count=1, hours=40, basis="on_demand", margin_percent=20
    )

    assert result.rate_cents == 120
    assert result.subtotal_cents == 4800
    assert result.margin_cents == 960
    assert result.total_cents == 5760


def test_estimate_cost_spot_uses_the_min_spot_price(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)

    result = pricing.estimate_cost(
        db_session, accelerator_name="A100", accelerator_count=1, hours=10, basis="spot", margin_percent=0
    )

    assert result.rate_cents == 35
    assert result.total_cents == 350


def test_estimate_cost_unknown_gpu_key_is_a_validation_error(db_session: Session) -> None:
    with pytest.raises(ValidationFailed) as exc_info:
        pricing.estimate_cost(
            db_session, accelerator_name="NOPE", accelerator_count=1, hours=1, basis="on_demand", margin_percent=0
        )
    assert "estimator_gpu" in exc_info.value.errors


def test_interruptible_without_any_interruptible_price_is_estimated_on_demand(db_session: Session) -> None:
    # The catalog has no interruptible prices and Krater hasn't sampled this GPU: an on-demand upper bound, labelled.
    price = _seed_gpu_price(db_session, name="A100", count=1)
    price.spot_min_cents = None
    db_session.flush()

    result = pricing.estimate_cost(
        db_session, accelerator_name="A100", accelerator_count=1, hours=1, basis="spot", margin_percent=0
    )

    assert result.basis == "on_demand"
    assert result.spot_unavailable is True
    assert result.rate_cents == price.on_demand_median_cents
    assert result.as_dict()["spot_unavailable"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"accelerator_name": "", "accelerator_count": 1, "hours": 1, "basis": "on_demand", "margin_percent": 0},
        {"accelerator_name": "A100", "accelerator_count": 1, "hours": 0, "basis": "on_demand", "margin_percent": 0},
        {"accelerator_name": "A100", "accelerator_count": 1, "hours": 1, "basis": "bogus", "margin_percent": 0},
        {"accelerator_name": "A100", "accelerator_count": 1, "hours": 1, "basis": "on_demand", "margin_percent": -5},
    ],
)
def test_estimate_cost_validates_input(db_session: Session, kwargs: dict) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)
    with pytest.raises(ValidationFailed):
        pricing.estimate_cost(db_session, **kwargs)


# -- Krater's own Vast samples -----------------------------------------------------------------------------------

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _sample(session: Session, name: str, rental_type: str, median_microusd: int, *, hours_ago: float) -> None:
    from datetime import timedelta

    from krater.models import GpuPriceSample

    session.add(
        GpuPriceSample(
            sampled_at=NOW - timedelta(hours=hours_ago),
            gpu_name=name,
            rental_type=rental_type,
            num_gpus=1,
            disk_gb=32,
            offer_count=10,
            truncated=False,
            min_microusd=median_microusd // 2,
            p10_microusd=median_microusd - 1,
            median_microusd=median_microusd,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    ("catalog", "sample", "matches"),
    [
        ("RTX3060", "RTX 3060", True),
        ("A100", "A100 SXM4", True),
        ("A100", "A100 PCIE", True),
        ("H100", "H100 SXM", True),
        ("RTX6000-Ada", "RTX 6000Ada", True),
        ("RTXPRO6000WS", "RTX PRO 6000 WS", True),
        ("RTX5070", "RTX 5070 Ti", False),
        ("RTX4060", "RTX 4060 Ti", False),
        ("RTXA6000", "RTX A4000", False),
        ("L4", "L40S", False),
    ],
)
def test_catalog_and_vast_gpu_names_are_matched(catalog: str, sample: str, matches: bool) -> None:
    assert pricing.sample_name_matches(catalog, sample) is matches


def test_estimate_uses_krater_samples_once_there_is_a_day_of_them(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)
    for hours_ago, median in [(1, 90_000), (12, 110_000), (30, 100_000)]:
        _sample(db_session, "A100 SXM4", "bid", median, hours_ago=hours_ago)
    _sample(db_session, "A100 PCIE", "bid", 400_000, hours_ago=200)  # older than the week: left out

    result = pricing.estimate_cost(
        db_session, accelerator_name="A100", accelerator_count=2, hours=10, basis="spot", margin_percent=0, now=NOW
    )

    assert result.rate_source == pricing.SOURCE_SAMPLES
    assert result.rate_cents == 10  # the median $0.10/h, not the catalog's minimum
    assert result.total_cents == 200
    assert "Krater's Vast prices" in estimator_summary(result)


def test_estimate_falls_back_to_the_catalog_without_a_day_of_samples(db_session: Session) -> None:
    _seed_gpu_price(db_session, name="A100", count=1)
    _sample(db_session, "A100 SXM4", "ondemand", 900_000, hours_ago=1)
    _sample(db_session, "A100 SXM4", "ondemand", 900_000, hours_ago=5)

    result = pricing.estimate_cost(
        db_session, accelerator_name="A100", accelerator_count=1, hours=1, basis="on_demand", margin_percent=0, now=NOW
    )

    assert result.rate_source == pricing.SOURCE_CATALOG and result.rate_cents == 120


def test_an_estimate_stored_before_rate_sources_still_reads(db_session: Session) -> None:
    from krater.web.estimator_form import summary_text_from_dict

    old = {
        "accelerator_name": "A100",
        "accelerator_count": 1,
        "hours": 40,
        "basis": "on_demand",
        "margin_percent": 20,
        "rate_cents": 120,
        "subtotal_cents": 4800,
        "margin_cents": 960,
        "total_cents": 5760,
        "refreshed_at": "2026-09-27T07:00:00+00:00",
    }

    assert summary_text_from_dict(old).endswith("(prices from Sep 27)")


def estimator_summary(result) -> str:
    from krater.web.estimator_form import summary_text

    return summary_text(result)
