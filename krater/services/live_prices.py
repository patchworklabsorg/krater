"""Live price checks: what Vast offers right now for the shape of a job a member is about to queue.

The job form's prices are Krater's hourly sample (`krater.services.price_history`), and with only a few offers for a
GPU the cheapest can be gone in minutes; a member asked for the price now (2026-11-13). Asking for an estimate
(`request`) records the shape; the worker searches Vast once for it (`check`, at most every `QUOTE_FRESH`), with the
scheduler's own query (`krater.compute.offers.shape_query`) and pricing (bid margin, disk, transfer cap), asking for
the cheapest few rows only. The page and the CLI poll for the answer (`view`).

Searches count against Vast's daily quota of search rows, which the scheduler needs to start jobs. A check is skipped
when less than `QUOTA_RESERVE` of the quota is left, or when Krater's own daily count (`DAILY_SEARCHES`, kept in
`live_price_usage`) is used up: that holds even when Vast's quota headers haven't been seen.

Vast is only called from the worker (its key never reaches the portal); the portal only reads and writes rows here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from krater.compute import offers
from krater.models import ComputeJobKind, LivePriceUsage, LiveQuote
from krater.providers import ComputeProvider, ProviderError
from krater.services.compute_settings import ComputePolicy
from krater.vast import RentalType, microusd_to_usd

#: A check this recent answers again without searching.
QUOTE_FRESH = timedelta(minutes=2)
#: A check asked for this recently and not yet done isn't asked for again (the worker is on it).
QUOTE_PENDING = timedelta(minutes=1)
#: Rows a check asks Vast for, cheapest first: the cheapest is in the first few once sorted by the price the rental
#: type pays, and rows are what the quota counts.
QUOTE_ROWS = 8
#: The share of Vast's daily search quota live checks leave for the scheduler (as the price history does).
QUOTA_RESERVE = 0.5
PURPOSE_QUOTE = "quote"
#: Krater's own cap on live searches a UTC day, by purpose: at most `QUOTE_ROWS` rows each.
DAILY_SEARCHES = {PURPOSE_QUOTE: 400}


@dataclass(frozen=True)
class Shape:
    """What a live check searches for: the job's shape, and the project's rules that decide what it would pay."""

    rental_type: RentalType
    gpu_names: tuple[str, ...]
    min_gpu_ram_gb: float | None
    num_gpus: int
    disk_gb: int
    box: bool
    reliability_floor: float
    transfer_cap_microusd_per_gb: int | None
    bid_margin_percent: int

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_json(cls, data: dict) -> Shape:
        return cls(**{**data, "gpu_names": tuple(data["gpu_names"])})


def shape_of(spec, policy: ComputePolicy) -> Shape:
    """The shape of a `JobSpec` under `policy` (the project's effective compute settings)."""
    return Shape(
        rental_type="bid" if spec.interruptible else "ondemand",
        gpu_names=tuple(sorted(spec.gpu_names)),
        min_gpu_ram_gb=spec.min_gpu_ram_gb,
        num_gpus=spec.num_gpus,
        disk_gb=spec.disk_gb,
        box=spec.kind is ComputeJobKind.BOX,
        reliability_floor=policy.reliability_floor,
        transfer_cap_microusd_per_gb=policy.transfer_cap_microusd_per_gb,
        bid_margin_percent=policy.bid_margin_percent,
    )


def request(session: Session, shape: Shape, *, now: datetime) -> tuple[LiveQuote, bool]:
    """The quote for `shape`, created if new, and whether the worker should check it now (`False` while a check is
    fresh, or one was just asked for). Flushes; the caller commits before deferring the check."""
    row = session.scalars(sa.select(LiveQuote).where(LiveQuote.key == shape.key)).one_or_none()
    if row is None:
        row = LiveQuote(key=shape.key, shape=asdict(shape), requested_at=now)
        session.add(row)
        session.flush()
        return row, True
    if row.checked_at is not None and now - row.checked_at < QUOTE_FRESH:
        return row, False
    pending = (row.checked_at is None or row.checked_at < row.requested_at) and now - row.requested_at < QUOTE_PENDING
    if pending:
        return row, False
    row.requested_at = now
    session.flush()
    return row, True


def find(session: Session, key: str) -> LiveQuote | None:
    return session.scalars(sa.select(LiveQuote).where(LiveQuote.key == key)).one_or_none()


def _used_today(session: Session, purpose: str, *, now: datetime) -> int:
    used = session.scalar(
        sa.select(LivePriceUsage.searches).where(LivePriceUsage.day == now.date(), LivePriceUsage.purpose == purpose)
    )
    return used or 0


def count_searches(session: Session, purpose: str, searches: int, *, now: datetime) -> None:
    """Add `searches` to today's count for `purpose` (an upsert, so concurrent checks don't lose one)."""
    statement = insert(LivePriceUsage).values(day=now.date(), purpose=purpose, searches=searches)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["day", "purpose"], set_={"searches": LivePriceUsage.searches + searches}
        )
    )


def refusal(session: Session, client: ComputeProvider, purpose: str, searches: int, *, now: datetime) -> str | None:
    """Why live searches for `purpose` mustn't run now, or `None`: Vast's quota under `QUOTA_RESERVE`, or Krater's
    own daily count used up."""
    quota = client.search_quota
    if quota is not None and quota.fraction_left(now) < QUOTA_RESERVE:
        return "Krater is saving today's Vast searches for starting jobs, so it can't check live prices now."
    if _used_today(session, purpose, now=now) + searches > DAILY_SEARCHES[purpose]:
        return "Krater has used today's live price checks. They're back at midnight UTC."
    return None


def check(session: Session, client: ComputeProvider, key: str, *, now: datetime) -> LiveQuote | None:
    """Search Vast for the quote `key` names, unless it was checked within `QUOTE_FRESH`, and record the cheapest
    offers Krater would rent at what Krater would pay. Flushes; the caller commits."""
    row = session.scalars(sa.select(LiveQuote).where(LiveQuote.key == key)).one_or_none()
    if row is None or (row.checked_at is not None and now - row.checked_at < QUOTE_FRESH):
        return row
    shape = Shape.from_json(row.shape)
    why_not = refusal(session, client, PURPOSE_QUOTE, 1, now=now)
    if why_not is not None:
        row.error, row.checked_at, row.cheapest_microusd, row.offers = why_not, now, None, []
        session.flush()
        return row
    query = offers.shape_query(
        rental_type=shape.rental_type,
        gpu_names=shape.gpu_names,
        min_gpu_ram_gb=shape.min_gpu_ram_gb,
        num_gpus=shape.num_gpus,
        disk_gb=shape.disk_gb,
        box=shape.box,
        reliability_floor=shape.reliability_floor,
        transfer_cap_microusd_per_gb=shape.transfer_cap_microusd_per_gb,
        limit=QUOTE_ROWS,
        order_by_price=True,
    )
    count_searches(session, PURPOSE_QUOTE, 1, now=now)
    try:
        found = client.search_offers(query)
    except ProviderError:
        row.error, row.checked_at, row.cheapest_microusd, row.offers = (
            "Vast didn't answer Krater's search. Try again in a minute.",
            now,
            None,
            [],
        )
        session.flush()
        return row
    priced = sorted(
        (
            choice
            for offer in found
            if (
                choice := offers.price(
                    offer,
                    rental_type=shape.rental_type,
                    disk_gb=shape.disk_gb,
                    margin_percent=shape.bid_margin_percent,
                    max_microusd=10**12,
                    transfer_cap_microusd_per_gb=shape.transfer_cap_microusd_per_gb,
                )
            )
        ),
        key=lambda choice: choice.full_microusd,
    )
    row.offers = [{"gpu": choice.offer.gpu_name, "full_microusd": choice.full_microusd} for choice in priced[:3]]
    row.cheapest_microusd = priced[0].full_microusd if priced else None
    row.error = None
    row.checked_at = now
    session.flush()
    return row


def _rate(microusd: int) -> str:
    return f"${microusd_to_usd(microusd):.4f}/h"


def view(row: LiveQuote) -> dict:
    """The quote as the page and the CLI show it: `status` (`checking`, `ready` or `unavailable`) and `text`."""
    if row.checked_at is None or row.checked_at < row.requested_at:
        return {"status": "checking", "text": "Checking Vast's prices for this job now…"}
    if row.error:
        return {"status": "unavailable", "text": row.error, "checked_at": row.checked_at.isoformat()}
    clock = row.checked_at.strftime("%H:%M:%S UTC")
    shape = Shape.from_json(row.shape)
    if row.cheapest_microusd is None:
        text = f"Right now on Vast (checked {clock}) no offer fits this job."
    else:
        rental = "interruptible" if shape.rental_type == "bid" else "on demand"
        others = ", ".join(_rate(offer["full_microusd"]) for offer in row.offers[1:])
        gpu = f" ({row.offers[0]['gpu']})" if len(shape.gpu_names) != 1 and row.offers else ""
        text = (
            f"Right now on Vast (checked {clock}) the cheapest {rental} offer that fits this job costs "
            f"{_rate(row.cheapest_microusd)}{gpu}"
            + (f"; the next cost {others}" if others else "")
            + ". Krater rents what's cheapest when the job starts, which can differ by then."
        )
        if len(shape.gpu_names) != 1:
            text += " With several GPU models allowed, it rents the best performance per dollar, which may cost more."
    return {
        "status": "ready",
        "text": text,
        "checked_at": row.checked_at.isoformat(),
        "cheapest_usd_per_hour": None
        if row.cheapest_microusd is None
        else f"{microusd_to_usd(row.cheapest_microusd):.4f}",
    }


__all__ = [
    "DAILY_SEARCHES",
    "PURPOSE_QUOTE",
    "QUOTA_RESERVE",
    "QUOTE_FRESH",
    "QUOTE_ROWS",
    "Shape",
    "check",
    "count_searches",
    "find",
    "refusal",
    "request",
    "shape_of",
    "view",
]
