"""Vast GPU pricing: refresh from the public Vast catalog (`krater.pricing.catalog`), aggregate per accelerator, and
serve `/pricing` plus the proposal-form budget estimator. See `docs/dev/pricing.md` for where the numbers come from
(SkyPilot's public catalog CSV on GitHub), how aggregation works, and the refresh/failure
story. Framework-free like every other service: no FastAPI here.

The estimator prefers Krater's own Vast price history (`krater.services.price_history`, sampled by the worker while
it has a Vast key) once a GPU has at least a day of samples in the last week: the median of the samples' medians, a
typical price rather than the catalog's cheapest offer. Otherwise it uses the catalog.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import GpuPrice, GpuPriceSample
from krater.pricing.catalog import FakeCatalog, GpuOffer, LiveCatalog
from krater.services.errors import ValidationFailed

#: `estimate_cost`'s `basis` values: which of a `GpuPrice` row's rates to price the estimate at.
BASIS_ON_DEMAND = "on_demand"
BASIS_SPOT = "spot"
VALID_BASES = (BASIS_ON_DEMAND, BASIS_SPOT)

#: Where an estimate's rate came from (`BudgetEstimate.rate_source`).
SOURCE_CATALOG = "catalog"
SOURCE_SAMPLES = "krater_samples"
# The team typed its own hourly price (a card Krater has no price for, or a price they expect to get).
SOURCE_MEMBER = "member"
#: Krater's samples are used once a GPU has at least `SAMPLE_MIN_SPAN` of them within `SAMPLE_WINDOW`.
SAMPLE_WINDOW = timedelta(days=7)
SAMPLE_MIN_SPAN = timedelta(days=1)
#: Vast names variants the catalog lumps together (A100 SXM4 and A100 PCIE are both the catalog's A100). Never "Ti":
#: an RTX 5070 Ti is a different GPU from an RTX 5070.
_VARIANT_SUFFIXES = ("sxm", "sxm4", "sxm5", "pcie", "nvl")

#: Sort keys accepted by `list_prices`' `sort` argument (also what `/pricing`'s `?sort=` reads).
_SORT_COLUMNS: dict[str, tuple] = {
    "name": (GpuPrice.accelerator_name, GpuPrice.accelerator_count),
    "price": (GpuPrice.on_demand_median_cents, GpuPrice.accelerator_name),
    "price_desc": (sa.desc(GpuPrice.on_demand_median_cents), GpuPrice.accelerator_name),
    "vram": (sa.desc(sa.func.coalesce(GpuPrice.vram_gib, -1)), GpuPrice.accelerator_name),
    "count": (GpuPrice.accelerator_count, GpuPrice.accelerator_name),
    "offers": (sa.desc(GpuPrice.offer_count), GpuPrice.accelerator_name),
}
DEFAULT_SORT = "name"


def _cents(dollars: float) -> int:
    return round(dollars * 100)


@dataclass(frozen=True)
class _Aggregate:
    accelerator_name: str
    accelerator_count: int
    vram_gib: float | None
    vcpus_typical: float | None
    memory_gib_typical: float | None
    on_demand_min_cents: int
    on_demand_median_cents: int
    spot_min_cents: int | None
    offer_count: int


def aggregate_offers(offers: list[GpuOffer]) -> list[_Aggregate]:
    """Group `offers` by `(accelerator_name, accelerator_count)` and reduce each group to one row.

    See `docs/dev/pricing.md` for why: median (not mean) for "typical" specs, min *and* median for
    on-demand price, min-of-positive-only for spot (a `0.0` spot price in the source means "not
    quoted", not a real free offer). An offer with a non-positive on-demand price is dropped outright
    -- not a usable quote either way.
    """
    groups: dict[tuple[str, int], list[GpuOffer]] = {}
    for offer in offers:
        if offer.price_dollars <= 0:
            continue
        groups.setdefault((offer.accelerator_name, offer.accelerator_count), []).append(offer)

    aggregates: list[_Aggregate] = []
    for (name, count), rows in groups.items():
        prices = [row.price_dollars for row in rows]
        spot_prices = [row.spot_price_dollars for row in rows if row.spot_price_dollars > 0]
        vcpus = [row.vcpus for row in rows if row.vcpus is not None]
        memory = [row.memory_gib for row in rows if row.memory_gib is not None]
        vram = [row.device_memory_gib for row in rows if row.device_memory_gib is not None]
        aggregates.append(
            _Aggregate(
                accelerator_name=name,
                accelerator_count=count,
                vram_gib=round(statistics.median(vram), 1) if vram else None,
                vcpus_typical=statistics.median(vcpus) if vcpus else None,
                memory_gib_typical=round(statistics.median(memory), 1) if memory else None,
                on_demand_min_cents=_cents(min(prices)),
                on_demand_median_cents=_cents(statistics.median(prices)),
                spot_min_cents=_cents(min(spot_prices)) if spot_prices else None,
                offer_count=len(rows),
            )
        )
    return aggregates


def refresh_prices(session: Session, client: LiveCatalog | FakeCatalog) -> int:
    """Fetch the current catalog, aggregate it, and atomically replace every `GpuPrice` row. Returns
    the number of `(accelerator, count)` groups written.

    **Fails soft**: `client.list_gpu_prices()` (and `aggregate_offers`) run to completion *before* any
    database write, so a `CatalogError` from a bad fetch/parse propagates straight to the caller
    without touching existing rows -- the periodic task and CLI both leave last refresh's prices (and
    their `refreshed_at`) in place on failure. The delete + inserts below happen in the caller's own
    transaction (this only flushes; the caller commits), so a crash mid-write rolls back to the old
    rows too, never a half-replaced table.
    """
    offers = client.list_gpu_prices()
    aggregates = aggregate_offers(offers)
    refreshed_at = datetime.now(UTC)

    session.execute(sa.delete(GpuPrice))
    for agg in aggregates:
        session.add(
            GpuPrice(
                accelerator_name=agg.accelerator_name,
                accelerator_count=agg.accelerator_count,
                vram_gib=agg.vram_gib,
                vcpus_typical=agg.vcpus_typical,
                memory_gib_typical=agg.memory_gib_typical,
                on_demand_min_cents=agg.on_demand_min_cents,
                on_demand_median_cents=agg.on_demand_median_cents,
                spot_min_cents=agg.spot_min_cents,
                offer_count=agg.offer_count,
                refreshed_at=refreshed_at,
            )
        )
    session.flush()
    return len(aggregates)


# --------------------------------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------------------------------


def list_prices(session: Session, *, sort: str = DEFAULT_SORT, accelerator_name: str | None = None) -> list[GpuPrice]:
    """Every `GpuPrice` row, sorted by `sort` (falls back to `DEFAULT_SORT` for an unknown value --
    `/pricing`'s `?sort=` is user-controlled query-string input, not meant to ever 422)."""
    stmt = sa.select(GpuPrice)
    if accelerator_name:
        stmt = stmt.where(GpuPrice.accelerator_name == accelerator_name)
    stmt = stmt.order_by(*_SORT_COLUMNS.get(sort, _SORT_COLUMNS[DEFAULT_SORT]))
    return list(session.scalars(stmt))


def list_accelerator_names(session: Session) -> list[str]:
    """Every distinct accelerator name currently priced, alphabetically -- for `/pricing`'s filter and
    the estimator's GPU dropdown."""
    return list(session.scalars(sa.select(GpuPrice.accelerator_name).distinct().order_by(GpuPrice.accelerator_name)))


def get_price(session: Session, *, accelerator_name: str, accelerator_count: int) -> GpuPrice | None:
    return session.scalar(
        sa.select(GpuPrice).where(
            GpuPrice.accelerator_name == accelerator_name, GpuPrice.accelerator_count == accelerator_count
        )
    )


def last_refreshed_at(session: Session) -> datetime | None:
    """When the current price set was refreshed, or `None` before the first refresh has ever run."""
    return session.scalar(sa.select(sa.func.max(GpuPrice.refreshed_at)))


def gpu_key(accelerator_name: str, accelerator_count: int) -> str:
    """The `"<name>:<count>"` value used in `<select>` options and posted form fields -- see
    `parse_gpu_key`."""
    return f"{accelerator_name}:{accelerator_count}"


def parse_gpu_key(key: str) -> tuple[str, int] | None:
    """Parse a `gpu_key`-shaped string back into `(accelerator_name, accelerator_count)`, or `None` if
    it isn't one (never raises -- callers turn `None` into a field error with their own wording)."""
    name, _, count_raw = key.rpartition(":")
    if not name or not count_raw.isdigit():
        return None
    return name, int(count_raw)


# --------------------------------------------------------------------------------------------------
# Budget estimator
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetEstimate:
    """The result of `estimate_cost`: what the proposal form's estimator shows, and what gets stored
    verbatim (via `as_dict`) on `ProjectRevision.budget_estimate` when the submitter uses it. Every
    field here was computed by the server from its own current `GpuPrice` row -- never trust a
    client-sent rate or total (see `docs/dev/pricing.md`)."""

    accelerator_name: str
    accelerator_count: int
    hours: float
    basis: str
    margin_percent: int
    rate_cents: int
    subtotal_cents: int
    margin_cents: int
    total_cents: int
    refreshed_at: datetime
    # `SOURCE_CATALOG` or `SOURCE_SAMPLES`. Estimates stored before this field read as the catalog.
    rate_source: str = SOURCE_CATALOG
    # Interruptible was asked for but Krater has no interruptible price for this GPU yet, so `basis` is on demand.
    spot_unavailable: bool = False
    # With `SOURCE_MEMBER`: what Krater's own prices say per GPU-hour, for reviewers to compare; `None` if it has none.
    market_rate_cents: int | None = None
    # The team's own price per GPU-hour in micro-dollars, exactly as typed: bids are often a fraction of a cent
    # ($0.0587/h). `rate_cents` is that rounded, for display; the totals use this. `None` for Krater's rates.
    rate_microusd: int | None = None

    def as_dict(self) -> dict:
        return {
            "rate_source": self.rate_source,
            "spot_unavailable": self.spot_unavailable,
            "market_rate_cents": self.market_rate_cents,
            "rate_microusd": self.rate_microusd,
            "accelerator_name": self.accelerator_name,
            "accelerator_count": self.accelerator_count,
            "hours": self.hours,
            "basis": self.basis,
            "margin_percent": self.margin_percent,
            "rate_cents": self.rate_cents,
            "subtotal_cents": self.subtotal_cents,
            "margin_cents": self.margin_cents,
            "total_cents": self.total_cents,
            "refreshed_at": self.refreshed_at.isoformat(),
        }


def _normalized(name: str) -> str:
    return re.sub(r"[\s_-]", "", name.lower())


def sample_name_matches(catalog_name: str, sample_name: str) -> bool:
    """Whether Vast's `sample_name` is the catalog's `catalog_name` (spacing aside, or one of its variants)."""
    catalog, sample = _normalized(catalog_name), _normalized(sample_name)
    return sample == catalog or (sample.startswith(catalog) and sample[len(catalog) :] in _VARIANT_SUFFIXES)


def sampled_rate_cents(
    session: Session, *, accelerator_name: str, basis: str, now: datetime
) -> tuple[int, datetime] | None:
    """A typical full hourly price for one `accelerator_name` (32 GB disk included) from Krater's samples of the last
    `SAMPLE_WINDOW`, and when the newest was taken; `None` without `SAMPLE_MIN_SPAN` of samples."""
    since = now - SAMPLE_WINDOW
    names = [
        name
        for name in session.scalars(
            sa.select(GpuPriceSample.gpu_name).where(GpuPriceSample.sampled_at >= since).distinct()
        )
        if sample_name_matches(accelerator_name, name)
    ]
    if not names:
        return None
    rows = session.execute(
        sa.select(GpuPriceSample.median_microusd, GpuPriceSample.sampled_at).where(
            GpuPriceSample.gpu_name.in_(names),
            GpuPriceSample.rental_type == ("bid" if basis == BASIS_SPOT else "ondemand"),
            GpuPriceSample.num_gpus == 1,
            GpuPriceSample.sampled_at >= since,
            GpuPriceSample.median_microusd.is_not(None),
        )
    ).all()
    if not rows:
        return None
    times = [sampled_at for _, sampled_at in rows]
    if max(times) - min(times) < SAMPLE_MIN_SPAN:
        return None
    median_microusd = statistics.median(median for median, _ in rows)
    return math.ceil(median_microusd / 10_000), max(times)


@dataclass(frozen=True)
class _MarketRate:
    rate_cents: int
    refreshed_at: datetime
    source: str
    basis: str
    spot_unavailable: bool


def _market_rate(
    session: Session, *, accelerator_name: str, accelerator_count: int, basis: str, now: datetime
) -> _MarketRate | None:
    """Krater's price per GPU-hour: its own Vast samples, else the catalog. Interruptible falls back to on demand
    (`spot_unavailable`) when Krater has no interruptible price for the GPU. `None` when it has no price at all."""
    sampled = sampled_rate_cents(session, accelerator_name=accelerator_name, basis=basis, now=now)
    price = get_price(session, accelerator_name=accelerator_name, accelerator_count=accelerator_count)
    spot_unavailable = False
    if basis == BASIS_SPOT and sampled is None and (price is None or price.spot_min_cents is None):
        basis, spot_unavailable = BASIS_ON_DEMAND, True
        sampled = sampled_rate_cents(session, accelerator_name=accelerator_name, basis=BASIS_ON_DEMAND, now=now)
    if sampled is not None:
        rate_cents, refreshed_at = sampled
        return _MarketRate(rate_cents, refreshed_at, SOURCE_SAMPLES, basis, spot_unavailable)
    if price is None:
        return None
    rate_cents = price.spot_min_cents if basis == BASIS_SPOT else price.on_demand_median_cents
    return _MarketRate(rate_cents, price.refreshed_at, SOURCE_CATALOG, basis, spot_unavailable)


def estimate_cost(
    session: Session,
    *,
    accelerator_name: str,
    accelerator_count: int,
    hours: float,
    basis: str,
    margin_percent: int,
    rate_override_microusd: int | None = None,
    now: datetime | None = None,
) -> BudgetEstimate:
    """Recompute a budget estimate from Krater's own Vast samples, or else its current `GpuPrice` (catalog) row.

    Interruptible (`BASIS_SPOT`, Krater's default) prices at the typical bid Krater sampled. SkyPilot's catalog has no
    interruptible prices, so a GPU Krater hasn't sampled yet is priced on demand instead, and the estimate says so
    (`spot_unavailable`): an upper bound, never a refusal.

    `rate_override_microusd` is the team's own price per GPU-hour, in micro-dollars. It replaces Krater's rate
    (`SOURCE_MEMBER`), works for a card Krater has no price for, and keeps Krater's rate beside it
    (`market_rate_cents`) for reviewers.

    Raises `ValidationFailed` (field name -> message, matching every other form-validating service
    call) for bad input or an accelerator/basis Krater has no current pricing for -- callers show these
    as field errors next to the estimator's own inputs, distinct from the budget field itself.
    """
    errors: dict[str, str] = {}
    if not accelerator_name:
        errors["estimator_gpu"] = "Choose a GPU type."
    if accelerator_count <= 0:
        errors["estimator_gpu"] = "Choose a GPU type."
    if hours <= 0:
        errors["estimator_hours"] = "Enter the number of hours."
    if basis not in VALID_BASES:
        errors["estimator_basis"] = "Choose a pricing basis."
    if margin_percent < 0:
        errors["estimator_margin_percent"] = "Safety margin can't be negative."
    if rate_override_microusd is not None and rate_override_microusd <= 0:
        errors["estimator_rate"] = "Enter an hourly price above zero, or leave it blank."
    if errors:
        raise ValidationFailed(errors)

    now = now or datetime.now(UTC)
    market = _market_rate(
        session, accelerator_name=accelerator_name, accelerator_count=accelerator_count, basis=basis, now=now
    )
    rate_microusd = None
    if rate_override_microusd is not None:
        rate_microusd = rate_override_microusd
        rate_cents, refreshed_at, rate_source, spot_unavailable = (
            round(rate_microusd / 10_000),
            now,
            SOURCE_MEMBER,
            False,
        )
        market_rate_cents = market.rate_cents if market is not None and market.basis == basis else None
    elif market is None:
        raise ValidationFailed(
            {"estimator_gpu": f"No current pricing for {accelerator_name}×{accelerator_count}: enter an hourly price."}
        )
    else:
        rate_cents, refreshed_at, rate_source = market.rate_cents, market.refreshed_at, market.source
        basis, spot_unavailable, market_rate_cents = market.basis, market.spot_unavailable, None

    if rate_microusd is not None:
        subtotal_cents = round(rate_microusd * accelerator_count * hours / 10_000)
    else:
        subtotal_cents = round(rate_cents * accelerator_count * hours)
    margin_cents = round(subtotal_cents * margin_percent / 100)

    return BudgetEstimate(
        accelerator_name=accelerator_name,
        accelerator_count=accelerator_count,
        hours=hours,
        basis=basis,
        margin_percent=margin_percent,
        rate_cents=rate_cents,
        subtotal_cents=subtotal_cents,
        margin_cents=margin_cents,
        total_cents=subtotal_cents + margin_cents,
        refreshed_at=refreshed_at,
        rate_source=rate_source,
        spot_unavailable=spot_unavailable,
        market_rate_cents=market_rate_cents,
        rate_microusd=rate_microusd,
    )


__all__ = [
    "BASIS_ON_DEMAND",
    "BASIS_SPOT",
    "SOURCE_CATALOG",
    "SOURCE_MEMBER",
    "SOURCE_SAMPLES",
    "VALID_BASES",
    "BudgetEstimate",
    "aggregate_offers",
    "estimate_cost",
    "get_price",
    "gpu_key",
    "last_refreshed_at",
    "list_accelerator_names",
    "list_prices",
    "parse_gpu_key",
    "refresh_prices",
    "sample_name_matches",
    "sampled_rate_cents",
]
