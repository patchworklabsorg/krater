"""Live price checks: what Vast offers right now for the shape of a job a member is about to queue.

The job form's prices are Krater's hourly sample (`krater.services.price_history`), and with only a few offers for a
GPU the cheapest can be gone in minutes; a member asked for the price now (2026-11-13). Asking for an estimate
(`request`) records the shape; the worker searches Vast once for it (`check`, at most every `QUOTE_FRESH`), with the
scheduler's own query (`krater.compute.offers.shape_query`) and pricing (bid margin, disk, transfer cap), asking for
the cheapest few rows only. The page and the CLI poll for the answer (`view`).

Searches count against Vast's daily quota of search rows, which the scheduler needs to start jobs. A check is skipped
when less than `QUOTA_RESERVE` of the quota is left, or when Krater's own daily count (`DAILY_SEARCHES`, kept in
`live_price_usage`) is used up: that holds even when Vast's quota headers haven't been seen.

The form's price table also gets a "Cheapest now" column (`request_table`, `check_table`, `view_table`): opening the
form asks the worker to search each GPU model and rental type again, at most every `TABLE_FRESH`, a few rows each,
and the page fills the column in as answers come back. These count under their own daily cap.

Vast is only called from the worker (its key never reaches the portal); the portal only reads and writes rows here.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from krater.compute import offers
from krater.models import ComputeJobKind, LivePrice, LivePriceUsage, LiveQuote
from krater.providers import ComputeProvider, ProviderError, ProviderRateLimitedError
from krater.services import compute_settings
from krater.services.compute_settings import ComputePolicy
from krater.vast import RentalType, microusd_to_usd, usd_to_microusd

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
PURPOSE_TABLE = "table"
#: The form's "Cheapest now" column is searched again when the form is opened and it's older than this.
TABLE_FRESH = timedelta(minutes=5)
#: A refresh asked for this recently and not finished isn't asked for again (a whole table takes about a minute).
TABLE_PENDING = timedelta(minutes=3)
#: Rows a table search asks for, cheapest first: only the cheapest is shown.
TABLE_ROWS = 4
#: Seconds between a refresh's searches: Vast rate-limits bursts of about 30.
TABLE_PAUSE_SECONDS = 1.5
#: Krater's own cap on live searches a UTC day, by purpose: a quote is one search of `QUOTE_ROWS` rows (about 3,200
#: rows a day at most); a full table is a search of `TABLE_ROWS` rows per GPU model and rental type, so 864 is about
#: 24 refreshes of 18 models (about 3,500 rows). Vast's quota was 20,000 rows a day on staging.
DAILY_SEARCHES = {PURPOSE_QUOTE: 400, PURPOSE_TABLE: 864}


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


def _waiting(row: LivePrice) -> bool:
    return row.checked_at is None or row.checked_at < row.requested_at


def request_table(session: Session, gpu_names: list[str], rental_types: tuple[str, ...], *, now: datetime) -> bool:
    """Mark the form's "Cheapest now" rows stale since `TABLE_FRESH` as wanted, creating missing ones, and say whether
    the worker should refresh them now (`False` while all are fresh, or a refresh asked for within `TABLE_PENDING` is
    still going). Flushes; the caller commits before deferring the refresh."""
    rows = {(row.gpu_name, row.rental_type): row for row in session.scalars(sa.select(LivePrice))}
    stale: list[tuple[tuple[str, str], LivePrice | None]] = []
    for wanted in ((gpu, rental) for gpu in gpu_names for rental in rental_types):
        row = rows.get(wanted)
        if row is not None and row.checked_at is not None and now - row.checked_at < TABLE_FRESH:
            continue
        if row is not None and _waiting(row) and now - row.requested_at < TABLE_PENDING:
            return False
        stale.append((wanted, row))
    if not stale:
        return False
    for (gpu, rental), row in stale:
        if row is None:
            session.add(LivePrice(gpu_name=gpu, rental_type=rental, requested_at=now))
        else:
            row.requested_at = now
    session.flush()
    return True


def check_table(
    session: Session,
    client: ComputeProvider,
    *,
    disk_gb: int,
    clock: Callable[[], datetime],
    on_row: Callable[[], None] = lambda: None,
    pause_seconds: float = TABLE_PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Search Vast for every "Cheapest now" row that's waiting, `pause_seconds` apart, calling `on_row` (the worker
    commits there, so the page fills in as it goes). A quota refusal or a rate limit ends the run, marking the rows
    left with why; another failure marks only its row. Returns the searches made."""
    policy = compute_settings.global_policy(session)
    waiting = [
        row
        for row in session.scalars(sa.select(LivePrice).order_by(LivePrice.gpu_name, LivePrice.rental_type))
        if _waiting(row)
    ]
    searched = 0
    for i, row in enumerate(waiting):
        now = clock()
        why_not = refusal(session, client, PURPOSE_TABLE, 1, now=now)
        if why_not is not None:
            _give_up(waiting[i:], why_not, now=now)
            on_row()
            return searched
        if searched and pause_seconds:
            sleep(pause_seconds)
        query = offers.shape_query(
            rental_type=row.rental_type,
            gpu_names=(row.gpu_name,),
            min_gpu_ram_gb=None,
            num_gpus=1,
            disk_gb=disk_gb,
            box=False,
            reliability_floor=policy.reliability_floor,
            transfer_cap_microusd_per_gb=policy.transfer_cap_microusd_per_gb,
            limit=TABLE_ROWS,
            order_by_price=True,
        )
        count_searches(session, PURPOSE_TABLE, 1, now=now)
        searched += 1
        try:
            found = client.search_offers(query)
        except ProviderRateLimitedError:
            _give_up(waiting[i:], "Vast asked Krater to slow down. Try again in a few minutes.", now=clock())
            on_row()
            return searched
        except ProviderError:
            _give_up([row], "Vast didn't answer Krater's search.", now=clock())
            on_row()
            continue
        # The hourly table's measure (`price_history.summarize`): 1-GPU offers on hosts Krater would rent, GPU price
        # plus disk, no bid margin.
        costs = sorted(
            usd_to_microusd(offer.full_hourly(row.rental_type, disk_gb))
            for offer in found
            if offer.num_gpus == 1 and offers.within_transfer_cap(offer, policy.transfer_cap_microusd_per_gb)
        )
        row.cheapest_microusd = costs[0] if costs else None
        row.error = None
        row.checked_at = clock()
        session.flush()
        on_row()
    return searched


def _give_up(rows: list[LivePrice], why: str, *, now: datetime) -> None:
    for row in rows:
        row.error, row.checked_at, row.cheapest_microusd = why, now, None


def view_table(session: Session) -> dict:
    """The "Cheapest now" column as the form shows and polls it: `rows` by `"gpu|rental"`, each with `status`
    (`checking`, `ready` or `unavailable`), `text` and `at` (its check time), plus `note` (why rows weren't
    searched, if any) and `done`."""
    rows = {}
    note = None
    for row in session.scalars(sa.select(LivePrice)):
        if _waiting(row):
            shown = {"status": "checking", "text": "checking…", "at": ""}
        elif row.error:
            note = note or row.error
            shown = {"status": "unavailable", "text": "not checked", "at": row.checked_at.strftime("%H:%M:%S UTC")}
        else:
            text = "none" if row.cheapest_microusd is None else _rate(row.cheapest_microusd)
            shown = {"status": "ready", "text": text, "at": row.checked_at.strftime("%H:%M:%S UTC")}
        rows[f"{row.gpu_name}|{row.rental_type}"] = shown
    return {
        "rows": rows,
        "note": note,
        "done": all(shown["status"] != "checking" for shown in rows.values()),
    }


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
    "PURPOSE_TABLE",
    "QUOTA_RESERVE",
    "QUOTE_FRESH",
    "QUOTE_ROWS",
    "TABLE_FRESH",
    "Shape",
    "check",
    "check_table",
    "count_searches",
    "find",
    "refusal",
    "request",
    "request_table",
    "shape_of",
    "view",
    "view_table",
]
