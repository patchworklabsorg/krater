"""Tests for `/pricing`: rendering, the empty state, filtering/sorting, and cap marking. No auth --
these use the plain `client` fixture with nobody signed in."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import GpuPrice


def _seed(session: Session, *, name: str, count: int = 1, on_demand_min_cents: int, spot_min_cents=None) -> GpuPrice:
    price = GpuPrice(
        accelerator_name=name,
        accelerator_count=count,
        vram_gib=40.0,
        vcpus_typical=32.0,
        memory_gib_typical=128.0,
        on_demand_min_cents=on_demand_min_cents,
        on_demand_median_cents=on_demand_min_cents + 10,
        spot_min_cents=spot_min_cents,
        offer_count=2,
        refreshed_at=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
    )
    session.add(price)
    session.flush()
    return price


def test_pricing_page_shows_empty_state_before_first_refresh(client: TestClient) -> None:
    response = client.get("/pricing")

    assert response.status_code == 200
    assert "No pricing data yet" in response.text


def test_pricing_page_renders_rows(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="A100", on_demand_min_cents=100)

    response = client.get("/pricing")

    assert response.status_code == 200
    assert "A100" in response.text
    assert "$1.00/hr" in response.text
    assert "Last updated" in response.text


def test_pricing_page_marks_rows_over_the_per_machine_cap(client: TestClient, db_session: Session) -> None:
    # Default KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS is 500 ($5.00/hr).
    _seed(db_session, name="H200", count=8, on_demand_min_cents=2071)
    _seed(db_session, name="V100", count=1, on_demand_min_cents=18)

    response = client.get("/pricing")

    assert response.status_code == 200
    assert "Over Ganymede's default per-machine cap" in response.text


def test_pricing_page_filters_by_gpu(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="A100", on_demand_min_cents=100)
    _seed(db_session, name="H100", on_demand_min_cents=200)

    response = client.get("/pricing", params={"gpu": "H100"})

    assert response.status_code == 200
    assert "<td>H100</td>" in response.text
    assert "<td>A100</td>" not in response.text


def test_pricing_page_ignores_an_unknown_gpu_filter(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="A100", on_demand_min_cents=100)

    response = client.get("/pricing", params={"gpu": "NOT-A-REAL-GPU"})

    assert response.status_code == 200
    assert "A100" in response.text


def test_pricing_page_sorts_by_price(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="EXPENSIVE", on_demand_min_cents=1000)
    _seed(db_session, name="CHEAP", on_demand_min_cents=10)

    response = client.get("/pricing", params={"sort": "price"})

    assert response.status_code == 200
    assert response.text.index("CHEAP") < response.text.index("EXPENSIVE")


def test_pricing_page_shows_lowest_spot_when_available(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="A100", on_demand_min_cents=100, spot_min_cents=35)

    response = client.get("/pricing")

    assert "$0.35/hr" in response.text


def test_pricing_page_dash_when_one_gpu_lacks_a_spot_price(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="A100", on_demand_min_cents=100, spot_min_cents=35)
    _seed(db_session, name="H100", on_demand_min_cents=200, spot_min_cents=None)

    response = client.get("/pricing")

    assert "Typical interruptible" in response.text
    assert "–" in response.text


def test_pricing_page_hides_the_spot_column_when_no_gpu_has_one(client: TestClient, db_session: Session) -> None:
    # SkyPilot's Vast catalog currently has no spot prices at all; an all-dashes column is just noise.
    _seed(db_session, name="A100", on_demand_min_cents=100, spot_min_cents=None)

    response = client.get("/pricing")

    assert response.status_code == 200
    assert "Typical interruptible" not in response.text


def test_pricing_link_is_in_the_nav(client: TestClient) -> None:
    response = client.get("/")

    assert 'href="/pricing"' in response.text


def test_the_live_history_link_shows_only_when_it_can_have_data(client, monkeypatch) -> None:
    from krater.config import get_settings

    assert "/pricing/history" not in client.get("/pricing").text  # compute off, no samples
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    assert "/pricing/history" in client.get("/pricing").text


def test_the_interruptible_column_comes_from_krater_samples(client: TestClient, db_session: Session) -> None:
    # SkyPilot's catalog has no interruptible prices; Krater's own hourly samples do.
    from datetime import UTC, datetime, timedelta

    from krater.models import GpuPriceSample

    _seed(db_session, name="RTX 3090", on_demand_min_cents=20, spot_min_cents=None)
    now = datetime.now(UTC)
    for hours_ago in (30, 2):
        db_session.add(
            GpuPriceSample(
                sampled_at=now - timedelta(hours=hours_ago),
                gpu_name="RTX 3090",
                rental_type="bid",
                num_gpus=1,
                disk_gb=32,
                offer_count=20,
                truncated=False,
                min_microusd=100_000,
                p10_microusd=110_000,
                median_microusd=118_000,
            )
        )
    db_session.flush()

    page = client.get("/pricing").text

    assert "Typical interruptible" in page
    assert "$0.12/hr" in page
