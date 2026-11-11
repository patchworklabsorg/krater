"""What a compute job may cost before it runs, and what it cost, part by part.

Before: `estimate` turns a job's prices, runtime and data estimate into an upper bound ("up to $X at the max runtime",
since the max runtime is a limit, not a prediction) and, when Krater has price history for its GPU models, what
that shape costs per hour at recent prices. Shown on the job form (the "Estimate the cost" button), the job page,
the API and `krater submit --estimate`.

After: `breakdown` splits a job's spend into GPU, disk, data in (with the image pull) and data out, Krater's own
estimate (`ComputeInstance.est_*_microusd`) next to what Vast charged for each (`charged_items`). Shown on the job
page and in the job-ended notice.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeJob, GpuPriceSample

MICROUSD_PER_CENT = 10_000
#: How far back the price history is read for an estimate.
MARKET_WINDOW = timedelta(hours=24)


class _Shape(Protocol):
    gpu_names: tuple[str, ...] | list[str]
    num_gpus: int
    interruptible: bool
    max_microusd: int
    target_microusd: int
    max_runtime_minutes: int
    data_in_gb: int | None
    data_out_gb: int | None


@dataclass(frozen=True)
class MarketPrice:
    """Full hourly cost of this shape at recent prices: the cheapest seen, and the typical (median) offer."""

    low_microusd: int
    typical_microusd: int
    samples: int


@dataclass(frozen=True)
class JobEstimate:
    rental_type: str
    max_hourly_microusd: int
    runtime_hours: Decimal
    # The most the machine costs over the max runtime, at the job's max price.
    up_to_cents: int
    # The member's data estimate at the transfer price cap: the most it costs. 0 without an estimate.
    data_cents: int
    market: MarketPrice | None

    @property
    def total_up_to_cents(self) -> int:
        return self.up_to_cents + self.data_cents

    @property
    def market_above_max(self) -> bool:
        return self.market is not None and self.market.low_microusd > self.max_hourly_microusd

    def as_json(self) -> dict:
        return {
            "rental_type": self.rental_type,
            "max_hourly_microusd": self.max_hourly_microusd,
            "runtime_hours": float(self.runtime_hours),
            "up_to_cents": self.up_to_cents,
            "data_cents": self.data_cents,
            "total_up_to_cents": self.total_up_to_cents,
            "market": None
            if self.market is None
            else {
                "low_microusd": self.market.low_microusd,
                "typical_microusd": self.market.typical_microusd,
                "samples": self.market.samples,
            },
        }

    def summary(self) -> str:
        """One plain sentence or two, for the CLI and notices."""
        text = (
            f"Up to ${self.up_to_cents / 100:,.2f} if it runs its full {_hours(self.runtime_hours)} at its max price"
            + (f", plus up to ${self.data_cents / 100:,.2f} of data at the price cap" if self.data_cents else "")
            + ". Most jobs finish sooner and cost less."
        )
        if self.market is not None:
            text += (
                f" Recent prices for this shape: ${self.market.low_microusd / 1_000_000:.3f}/h cheapest, "
                f"${self.market.typical_microusd / 1_000_000:.3f}/h typical."
            )
            if self.market_above_max:
                text += " Even the cheapest is above its max price, so expect it to wait."
        return text


def _hours(hours: Decimal) -> str:
    value = float(hours)
    return f"{value:g} hour" + ("" if value == 1 else "s")


def market_price(session: Session, job: _Shape, *, now: datetime) -> MarketPrice | None:
    """The shape's recent full hourly cost from Krater's price history (1-GPU samples, scaled by the GPU count).
    `None` for a job that takes any GPU model by memory, or with no samples in the window."""
    names = list(job.gpu_names or [])
    if not names:
        return None
    rows = list(
        session.scalars(
            sa.select(GpuPriceSample).where(
                GpuPriceSample.gpu_name.in_(names),
                GpuPriceSample.rental_type == ("bid" if job.interruptible else "ondemand"),
                GpuPriceSample.num_gpus == 1,
                GpuPriceSample.sampled_at >= now - MARKET_WINDOW,
                GpuPriceSample.min_microusd.is_not(None),
            )
        )
    )
    if not rows:
        return None
    low = min(row.min_microusd for row in rows)
    typical = int(statistics.median(row.median_microusd or row.min_microusd for row in rows))
    scale = max(job.num_gpus, 1)
    return MarketPrice(low_microusd=low * scale, typical_microusd=typical * scale, samples=len(rows))


def estimate(session: Session, job: _Shape, *, transfer_cap_microusd_per_gb: int, now: datetime) -> JobEstimate:
    runtime_hours = Decimal(job.max_runtime_minutes) / 60
    up_to = math.ceil(job.max_microusd * job.max_runtime_minutes / 60 / MICROUSD_PER_CENT)
    data_gb = (job.data_in_gb or 0) + (job.data_out_gb or 0)
    data = math.ceil(data_gb * transfer_cap_microusd_per_gb / MICROUSD_PER_CENT)
    return JobEstimate(
        rental_type="bid" if job.interruptible else "ondemand",
        max_hourly_microusd=job.max_microusd,
        runtime_hours=runtime_hours,
        up_to_cents=up_to,
        data_cents=data,
        market=market_price(session, job, now=now),
    )


# -- What it cost --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostLine:
    label: str
    estimate_microusd: int
    # What Vast charged for it at the last hourly check; `None` before any check.
    charged_microusd: int | None


@dataclass(frozen=True)
class Breakdown:
    lines: tuple[CostLine, ...]
    # Krater's estimate split by part only covers machines rented since the split was kept (2026-11-12).
    split_known: bool
    charged: bool

    def part_cents(self) -> dict[str, int]:
        """Each part in cents, Vast's figure where it has one: for the job-ended notice."""
        return {
            line.label: math.ceil(
                (line.charged_microusd if line.charged_microusd is not None else line.estimate_microusd)
                / MICROUSD_PER_CENT
            )
            for line in self.lines
        }


#: Krater's parts against Vast's item types. Vast's `bwd` covers the image pull too.
_PARTS = (
    ("GPU", ("est_gpu_microusd",), ("gpu",)),
    ("Disk", ("est_disk_microusd",), ("disk",)),
    ("Data in (and image pulls)", ("est_image_microusd", "est_transfer_in_microusd"), ("bwd",)),
    ("Data out", ("est_transfer_out_microusd",), ("bwu",)),
)


def breakdown(job: ComputeJob) -> Breakdown:
    instances = list(job.instances)
    charged_any = any(row.charged_items for row in instances)
    known_types = {kind for _, _, kinds in _PARTS for kind in kinds}
    lines = []
    for label, est_fields, kinds in _PARTS:
        est = sum(getattr(row, f) or 0 for row in instances for f in est_fields)
        charged = (
            sum((row.charged_items or {}).get(kind, 0) for row in instances for kind in kinds) if charged_any else None
        )
        lines.append(CostLine(label, est, charged))
    if charged_any:
        other = sum(
            amount for row in instances for kind, amount in (row.charged_items or {}).items() if kind not in known_types
        )
        if other:
            lines.append(CostLine("Other", 0, other))
    split_known = any(line.estimate_microusd for line in lines) or not any(row.accrued_microusd for row in instances)
    return Breakdown(lines=tuple(lines), split_known=split_known, charged=charged_any)
