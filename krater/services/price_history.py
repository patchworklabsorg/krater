"""Krater's own Vast price history: what the GPUs members ask for cost, sampled every few minutes.

Members pick a target price per job ("start when an RTX 4090 bid is under $0.20/h"); this history is what they pick
it from, and later what Krater suggests targets from. Vast has nothing to offer here: its market metrics API answers
host accounts only, and bulk use of its public price feed needs a data license.

Each run searches Vast once per tracked GPU model and rental type, for 1-GPU offers on hosts at or above the global
reliability floor, and writes one `GpuPriceSample` with the min, p10 and median full hourly cost. Full cost is how
Krater compares offers everywhere: the GPU price (the minimum bid, or `dph_base` on demand) plus a fixed basis disk
at the host's storage price. Framework-free, like the other services; the worker's periodic task calls `record`.

Its searches count against Vast's daily quota of search rows, which the scheduler needs to start jobs. A run stops
when less than `QUOTA_RESERVE` of the quota is left, so the history never takes the scheduler's share.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import GpuPriceSample
from krater.providers import ComputeProvider, ProviderError, ProviderRateLimitedError
from krater.services import compute_settings
from krater.vast import Offer, OfferQuery, RentalType, usd_to_microusd
from krater.vast.types import SEARCH_RESULT_CAP

logger = logging.getLogger(__name__)

RENTAL_TYPES: tuple[RentalType, ...] = ("bid", "ondemand")
#: The share of Vast's daily search quota price history leaves for the scheduler.
QUOTA_RESERVE = 0.5


@dataclass(frozen=True)
class PriceSummary:
    offer_count: int
    truncated: bool
    min_microusd: int | None
    p10_microusd: int | None
    median_microusd: int | None


def _nearest_rank(sorted_values: list[int], percent: float) -> int:
    """The nearest-rank percentile: the smallest value with at least `percent`% of values at or below it."""
    rank = max(1, math.ceil(percent / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(offers: list[Offer], rental_type: RentalType, *, disk_gb: int, num_gpus: int = 1) -> PriceSummary:
    """Min, p10 and median full hourly cost of the `num_gpus`-GPU offers among `offers`."""
    costs = sorted(
        usd_to_microusd(offer.full_hourly(rental_type, disk_gb)) for offer in offers if offer.num_gpus == num_gpus
    )
    if not costs:
        return PriceSummary(0, False, None, None, None)
    return PriceSummary(
        offer_count=len(costs),
        truncated=len(offers) >= SEARCH_RESULT_CAP,
        min_microusd=costs[0],
        p10_microusd=_nearest_rank(costs, 10),
        median_microusd=_nearest_rank(costs, 50),
    )


@dataclass(frozen=True)
class RecordResult:
    written: int
    failed: int
    stopped_early: bool


def record(
    session: Session,
    client: ComputeProvider,
    *,
    gpu_names: list[str],
    disk_gb: int,
    now: datetime,
    pause_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> RecordResult:
    """Search and write one sample per (GPU model, rental type), `pause_seconds` apart. One model failing is logged
    and skipped; a rate limit, or Vast's search quota falling under `QUOTA_RESERVE`, stops the run (the next run
    starts over). Flushes; the caller commits."""
    reliability = compute_settings.global_policy(session).reliability_floor
    written = failed = 0
    for i, (gpu_name, rental_type) in enumerate((g, r) for g in gpu_names for r in RENTAL_TYPES):
        quota = client.search_quota
        if quota is not None and quota.fraction_left(now) < QUOTA_RESERVE:
            logger.warning(
                "krater.price_history: %d of %d Vast search rows left today; leaving them to the scheduler",
                quota.remaining,
                quota.limit,
            )
            session.flush()
            return RecordResult(written, failed, stopped_early=True)
        if i and pause_seconds:
            sleep(pause_seconds)
        query = OfferQuery(
            rental_type=rental_type,
            gpu_names=(gpu_name,),
            num_gpus=1,
            disk_gb=disk_gb,
            min_reliability=reliability,
        )
        try:
            offers = client.search_offers(query)
        except ProviderRateLimitedError:
            logger.warning("krater.price_history: Vast rate-limited the run after %d samples; stopping", written)
            session.flush()
            return RecordResult(written, failed + 1, stopped_early=True)
        except ProviderError:
            logger.exception("krater.price_history: searching %s (%s) failed; skipping", gpu_name, rental_type)
            failed += 1
            continue
        summary = summarize(offers, rental_type, disk_gb=disk_gb)
        if summary.truncated:
            logger.info("krater.price_history: %s (%s) hit Vast's 512-offer cap", gpu_name, rental_type)
        session.add(
            GpuPriceSample(
                sampled_at=now,
                gpu_name=gpu_name,
                rental_type=rental_type,
                num_gpus=1,
                disk_gb=disk_gb,
                offer_count=summary.offer_count,
                truncated=summary.truncated,
                min_microusd=summary.min_microusd,
                p10_microusd=summary.p10_microusd,
                median_microusd=summary.median_microusd,
            )
        )
        written += 1
    session.flush()
    return RecordResult(written, failed, stopped_early=False)


def series(
    session: Session, *, gpu_name: str, rental_type: RentalType, since: datetime, num_gpus: int = 1
) -> list[GpuPriceSample]:
    """One GPU model's samples since `since`, oldest first: what a member's price chart draws."""
    stmt = (
        sa.select(GpuPriceSample)
        .where(
            GpuPriceSample.gpu_name == gpu_name,
            GpuPriceSample.rental_type == rental_type,
            GpuPriceSample.num_gpus == num_gpus,
            GpuPriceSample.sampled_at >= since,
        )
        .order_by(GpuPriceSample.sampled_at)
    )
    return list(session.scalars(stmt))


def latest(session: Session) -> list[GpuPriceSample]:
    """The newest sample of every (GPU model, rental type) series, by model name."""
    newest = (
        sa.select(
            GpuPriceSample.gpu_name,
            GpuPriceSample.rental_type,
            sa.func.max(GpuPriceSample.sampled_at).label("sampled_at"),
        )
        .where(GpuPriceSample.num_gpus == 1)
        .group_by(GpuPriceSample.gpu_name, GpuPriceSample.rental_type)
        .subquery()
    )
    stmt = (
        sa.select(GpuPriceSample)
        .join(
            newest,
            sa.and_(
                GpuPriceSample.gpu_name == newest.c.gpu_name,
                GpuPriceSample.rental_type == newest.c.rental_type,
                GpuPriceSample.sampled_at == newest.c.sampled_at,
            ),
        )
        .where(GpuPriceSample.num_gpus == 1)
        .order_by(GpuPriceSample.gpu_name, GpuPriceSample.rental_type)
    )
    return list(session.scalars(stmt))
