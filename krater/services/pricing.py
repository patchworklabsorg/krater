"""Vast GPU pricing: refresh from SkyPilot's catalog, aggregate per accelerator, and serve `/pricing`
plus the proposal-form budget estimator. See `docs/dev/pricing.md` for where the numbers come from
(SkyPilot's public catalog CSV, not a live API server), how aggregation works, and the refresh/failure
story. Framework-free like every other service: no FastAPI here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import GpuPrice
from krater.services.errors import ValidationFailed
from krater.skypilot.client import SkyPilotClient
from krater.skypilot.types import GpuOffer

#: `estimate_cost`'s `basis` values: which of a `GpuPrice` row's rates to price the estimate at.
BASIS_ON_DEMAND = "on_demand"
BASIS_SPOT = "spot"
VALID_BASES = (BASIS_ON_DEMAND, BASIS_SPOT)

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


def refresh_prices(session: Session, client: SkyPilotClient) -> int:
    """Fetch the current catalog, aggregate it, and atomically replace every `GpuPrice` row. Returns
    the number of `(accelerator, count)` groups written.

    **Fails soft**: `client.list_gpu_prices()` (and `aggregate_offers`) run to completion *before* any
    database write, so a `SkyPilotError` from a bad fetch/parse propagates straight to the caller
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

    def as_dict(self) -> dict:
        return {
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


def estimate_cost(
    session: Session,
    *,
    accelerator_name: str,
    accelerator_count: int,
    hours: float,
    basis: str,
    margin_percent: int,
) -> BudgetEstimate:
    """Recompute a budget estimate from Krater's own current `GpuPrice` row.

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
    if errors:
        raise ValidationFailed(errors)

    price = get_price(session, accelerator_name=accelerator_name, accelerator_count=accelerator_count)
    if price is None:
        raise ValidationFailed({"estimator_gpu": f"No current pricing for {accelerator_name}×{accelerator_count}."})

    if basis == BASIS_SPOT:
        if price.spot_min_cents is None:
            raise ValidationFailed(
                {"estimator_basis": f"No spot pricing available for {accelerator_name}×{accelerator_count}."}
            )
        rate_cents = price.spot_min_cents
    else:
        rate_cents = price.on_demand_median_cents

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
        refreshed_at=price.refreshed_at,
    )


__all__ = [
    "BASIS_ON_DEMAND",
    "BASIS_SPOT",
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
]
