"""Fetches SkyPilot's real, public Vast GPU catalog CSV (see `docs/dev/pricing.md`) and checks it
parses into sane `GpuOffer`/`GpuPrice` data. Marked `@pytest.mark.live` (deselected by default; see
`pyproject.toml`'s `markers`) -- unlike the other `tests/live/` checks, this needs no running server or
credentials, just network access to GitHub, so it skips cleanly on any fetch failure rather than
requiring specific env vars to opt in.

Run directly with: `uv run pytest -m live tests/live/test_pricing_live.py -v`
"""

from __future__ import annotations

import pytest

from krater.config import Settings
from krater.services.pricing import aggregate_offers
from krater.skypilot.errors import SkyPilotError
from krater.skypilot.live import LiveSkyPilotClient

pytestmark = pytest.mark.live


def _real_client() -> LiveSkyPilotClient:
    # Only `skypilot_catalog_url` matters for `list_gpu_prices` -- no API URL/service token needed
    # (docs/dev/pricing.md: this never talks to a SkyPilot API server).
    return LiveSkyPilotClient(Settings(skypilot_mode="live", skypilot_api_url="http://unused.invalid"))


def test_real_vast_catalog_fetches_and_parses() -> None:
    client = _real_client()
    try:
        offers = client.list_gpu_prices()
    except SkyPilotError as exc:
        pytest.skip(f"could not reach SkyPilot's public Vast catalog: {exc}")

    assert offers, "expected at least one GPU offer from the real catalog"
    assert all(offer.price_dollars > 0 or offer.price_dollars == 0 for offer in offers)
    names = {offer.accelerator_name for offer in offers}
    # These have been in SkyPilot's Vast catalog since well before this was written; a real regression
    # (e.g. the schema version bumping and this URL 404ing) would show up as an empty `offers` above,
    # already asserted -- this only checks the shape looks like GPU names, not any specific one forever.
    assert any(name.isupper() or any(ch.isdigit() for ch in name) for name in names)


def test_real_vast_catalog_aggregates_without_crashing() -> None:
    client = _real_client()
    try:
        offers = client.list_gpu_prices()
    except SkyPilotError as exc:
        pytest.skip(f"could not reach SkyPilot's public Vast catalog: {exc}")

    aggregates = aggregate_offers(offers)

    assert aggregates
    for aggregate in aggregates:
        assert aggregate.on_demand_min_cents > 0
        assert aggregate.on_demand_median_cents >= aggregate.on_demand_min_cents
        assert aggregate.offer_count >= 1
