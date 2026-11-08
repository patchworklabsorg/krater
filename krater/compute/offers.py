"""Choosing an offer for a job: full hourly cost, the bid, and the ranking.

Full hourly cost is the GPU price (the bid for an interruptible rental, `dph_base` on demand) plus the job's disk at
the host's storage price. It is what the target, the max price and the project's hourly cap are all compared with.

Data transfer is billed per GB on top of it, at the host's own prices. No host above the transfer price cap is ever
chosen, and when the member estimated the data their job moves, the ranking adds what that would cost at each host,
spread over the job's max runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from krater.vast import Offer, RentalType, microusd_to_usd, usd_to_microusd

#: Bids are sent with this many decimal places (Vast takes dollars per hour as a JSON number).
BID_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class Choice:
    offer: Offer
    rental_type: RentalType
    bid: Decimal | None  # dollars per hour, for an interruptible rental
    gpu_microusd: int
    disk_microusd: int

    @property
    def full_microusd(self) -> int:
        return self.gpu_microusd + self.disk_microusd


@dataclass(frozen=True)
class Transfer:
    """The data a job is expected to move, for ranking hosts by what it would cost there. Zero GB both ways
    (no estimate) ranks by the hourly cost alone, with transfer prices only breaking ties."""

    in_gb: int = 0
    out_gb: int = 0
    runtime_hours: Decimal = Decimal(1)

    def cost_microusd(self, offer: Offer) -> int:
        return self.in_gb * offer.transfer_in_microusd_per_gb + self.out_gb * offer.transfer_out_microusd_per_gb

    def hourly_microusd(self, offer: Offer) -> int:
        """The estimated transfer cost at `offer`, spread over the runtime."""
        return int(Decimal(self.cost_microusd(offer)) / max(self.runtime_hours, Decimal("0.01")))


NO_TRANSFER = Transfer()


def within_transfer_cap(offer: Offer, cap_microusd_per_gb: int | None) -> bool:
    """Whether `offer`'s prices for data in and out are both at or under the cap (`None`: no cap)."""
    if cap_microusd_per_gb is None:
        return True
    return (
        offer.transfer_in_microusd_per_gb <= cap_microusd_per_gb
        and offer.transfer_out_microusd_per_gb <= cap_microusd_per_gb
    )


def bid_for(offer: Offer, *, margin_percent: int, max_gpu: Decimal) -> Decimal | None:
    """The offer's minimum bid plus the margin, rounded up to `BID_QUANTUM`, and never above `max_gpu`. `None` when
    even the minimum bid is over `max_gpu`."""
    if offer.min_bid is None:
        return None
    bid = (offer.min_bid * (100 + margin_percent) / 100).quantize(BID_QUANTUM, rounding=ROUND_CEILING)
    if bid > max_gpu:
        bid = max_gpu.quantize(BID_QUANTUM, rounding=ROUND_FLOOR)
    if bid < offer.min_bid:
        return None
    return bid


def price(
    offer: Offer,
    *,
    rental_type: RentalType,
    disk_gb: int,
    margin_percent: int,
    max_microusd: int,
    transfer_cap_microusd_per_gb: int | None = None,
) -> Choice | None:
    """What renting `offer` would cost per hour, or `None` if it can't be rented within `max_microusd`, or its
    data transfer prices are over the cap. The cap is checked here as well as in the search: an offer Vast's filter
    let through is never rented."""
    if not within_transfer_cap(offer, transfer_cap_microusd_per_gb):
        return None
    disk_microusd = usd_to_microusd(offer.disk_hourly(disk_gb))
    if rental_type == "bid":
        max_gpu = microusd_to_usd(max_microusd - disk_microusd)
        bid = bid_for(offer, margin_percent=margin_percent, max_gpu=max_gpu)
        if bid is None:
            return None
        gpu_microusd = usd_to_microusd(bid)
    else:
        bid = None
        gpu_microusd = usd_to_microusd(offer.dph_base)
    choice = Choice(offer, rental_type, bid, gpu_microusd, disk_microusd)
    return choice if choice.full_microusd <= max_microusd else None


def _perf_per_dollar(choice: Choice, transfer: Transfer) -> float:
    # Vast reports deep-learning performance per dollar of its own dph_total; turn it back into performance and
    # divide by what Krater would actually pay, the expected transfer included.
    offer = choice.offer
    paid = choice.full_microusd + transfer.hourly_microusd(offer)
    if not offer.dlperf_per_dphtotal or paid <= 0:
        return 0.0
    dlperf = offer.dlperf_per_dphtotal * float(offer.dph_total)
    return dlperf / paid


def _transfer_tiebreak(offer: Offer) -> int:
    return offer.transfer_in_microusd_per_gb + offer.transfer_out_microusd_per_gb


def choose(
    offers: list[Offer],
    *,
    rental_type: RentalType,
    disk_gb: int,
    margin_percent: int,
    limit_microusd: int,
    max_microusd: int,
    several_models: bool,
    exclude: set[str] = frozenset(),  # type: ignore[assignment]
    transfer_cap_microusd_per_gb: int | None = None,
    transfer: Transfer = NO_TRANSFER,
) -> Choice | None:
    """The offer to rent: every offer whose full hourly cost is at or under `limit_microusd` (and so under the max)
    and whose transfer prices are under the cap, then the cheapest with the expected transfer counted, or, when the
    job allows several GPU models, the best performance per dollar."""
    candidates = []
    for offer in offers:
        if offer.id in exclude:
            continue
        choice = price(
            offer,
            rental_type=rental_type,
            disk_gb=disk_gb,
            margin_percent=margin_percent,
            max_microusd=min(max_microusd, limit_microusd),
            transfer_cap_microusd_per_gb=transfer_cap_microusd_per_gb,
        )
        if choice is not None:
            candidates.append(choice)
    if not candidates:
        return None
    if several_models:
        return max(
            candidates,
            key=lambda c: (_perf_per_dollar(c, transfer), -c.full_microusd, -_transfer_tiebreak(c.offer)),
        )
    return min(
        candidates,
        key=lambda c: (c.full_microusd + transfer.hourly_microusd(c.offer), _transfer_tiebreak(c.offer), c.offer.id),
    )


def cheapest(
    offers: list[Offer],
    *,
    rental_type: RentalType,
    disk_gb: int,
    margin_percent: int,
    transfer_cap_microusd_per_gb: int | None = None,
) -> int | None:
    """The lowest full hourly cost Krater would pay among `offers` (bid margin included, hosts over the transfer
    cap left out), for telling a waiting member where prices are."""
    costs = [
        c.full_microusd
        for o in offers
        if (
            c := price(
                o,
                rental_type=rental_type,
                disk_gb=disk_gb,
                margin_percent=margin_percent,
                max_microusd=10**12,
                transfer_cap_microusd_per_gb=transfer_cap_microusd_per_gb,
            )
        )
    ]
    return min(costs) if costs else None
