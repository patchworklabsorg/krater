"""Data carried in and out of the Vast client.

Vast quotes money as JSON floats in dollars (per hour for prices, per GB-month for storage, per GB for
bandwidth). The client parses each one with `Decimal(str(x))`, so nothing downstream does float arithmetic on
money. Hourly *rates* need sub-cent precision (a bid is often $0.0587/h, and a 10% margin on it is half a cent), so
Krater stores rates as integer micro-dollars per hour (`*_microusd` columns; `usd_to_microusd`). Budgets and spend
totals stay in integer cents.

The facts these shapes follow were checked against the live API in the 2026-10-11 spike (see
docs/compute/vast-api.md).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from krater.providers.phase import Phase

RentalType = Literal["bid", "ondemand"]

#: Hours in the month Vast's per-GB-month storage prices assume (its own UI divides by 730).
HOURS_PER_MONTH = Decimal(730)

#: Vast's `onstart` limit (its docs say 4048 characters).
ONSTART_MAX_CHARS = 4048

#: Vast returns at most this many offers per search, whatever `limit` asks for (measured 2026-10-11).
SEARCH_RESULT_CAP = 512

MICROUSD_PER_USD = Decimal(1_000_000)


def vast_id(value: str | int) -> int:
    """A provider id Krater holds as a string, as the integer Vast's paths and filters take. Anything but plain
    digits is refused, so an id from elsewhere can never change the path a request goes to."""
    text = str(value)
    if not (text.isascii() and text.isdigit()) or len(text) > 19:
        raise ValueError(f"not a Vast id: {text!r}")
    return int(text)


def usd_to_microusd(dollars: Decimal) -> int:
    """Dollars (a `Decimal`) to integer micro-dollars, rounding half up."""
    return int((dollars * MICROUSD_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def microusd_to_usd(microusd: int) -> Decimal:
    return Decimal(microusd) / MICROUSD_PER_USD


def dec(value: Any) -> Decimal | None:
    """A JSON number from Vast as a `Decimal`, through `str` so a float's binary noise isn't carried over.
    `None` (or a missing field) stays `None`."""
    if value is None or isinstance(value, bool):
        return None
    return Decimal(str(value))


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _id(value: Any) -> str | None:
    """One of Vast's integer ids as the string Krater keeps for every provider's ids."""
    number = _int(value)
    return None if number is None else str(number)


def _timestamp(value: Any) -> datetime | None:
    seconds = _float(value)
    return None if seconds is None else datetime.fromtimestamp(seconds, tz=UTC)


@dataclass(frozen=True)
class Offer:
    """One rentable offer from a search (`POST /api/v0/bundles/`).

    `dph_total` is the GPU price (`dph_base`) **plus disk at the search's `allocated_storage`**; `min_bid` is
    the GPU price alone. Never compare one with the other: use `gpu_price` and `full_hourly` instead.
    """

    id: str
    machine_id: str | None
    gpu_name: str
    num_gpus: int
    gpu_ram_mb: int | None
    gpu_frac: float | None
    dph_base: Decimal
    dph_total: Decimal
    min_bid: Decimal | None
    storage_cost: Decimal  # dollars per GB-month
    inet_up_cost: Decimal  # dollars per GB
    inet_down_cost: Decimal  # dollars per GB
    reliability: float | None
    dlperf_per_dphtotal: float | None
    cuda_max_good: float | None
    direct_port_count: int
    disk_space: float | None  # GB free on the host
    geolocation: str | None
    verified: bool

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Offer:
        return cls(
            id=str(int(raw["id"])),
            machine_id=_id(raw.get("machine_id")),
            gpu_name=str(raw.get("gpu_name") or ""),
            num_gpus=_int(raw.get("num_gpus")) or 0,
            gpu_ram_mb=_int(raw.get("gpu_ram")),
            gpu_frac=_float(raw.get("gpu_frac")),
            dph_base=dec(raw.get("dph_base")) or Decimal(0),
            dph_total=dec(raw.get("dph_total")) or Decimal(0),
            min_bid=dec(raw.get("min_bid")),
            storage_cost=dec(raw.get("storage_cost")) or Decimal(0),
            inet_up_cost=dec(raw.get("inet_up_cost")) or Decimal(0),
            inet_down_cost=dec(raw.get("inet_down_cost")) or Decimal(0),
            reliability=_float(raw.get("reliability")),
            dlperf_per_dphtotal=_float(raw.get("dlperf_per_dphtotal")),
            cuda_max_good=_float(raw.get("cuda_max_good")),
            direct_port_count=_int(raw.get("direct_port_count")) or 0,
            disk_space=_float(raw.get("disk_space")),
            geolocation=raw.get("geolocation"),
            verified=raw.get("verification") == "verified" if "verification" in raw else bool(raw.get("verified")),
        )

    def gpu_price(self, rental_type: RentalType) -> Decimal:
        """The GPU's own hourly price: the minimum bid for an interruptible rental, `dph_base` on demand."""
        if rental_type == "bid":
            return self.min_bid if self.min_bid is not None else self.dph_base
        return self.dph_base

    def disk_hourly(self, disk_gb: int | Decimal) -> Decimal:
        return self.storage_cost * Decimal(disk_gb) / HOURS_PER_MONTH

    def full_hourly(self, rental_type: RentalType, disk_gb: int | Decimal, *, bid: Decimal | None = None) -> Decimal:
        """GPU plus disk per hour, comparable across offers and rental types. Bandwidth is per GB used, so it's
        left out here. `bid` replaces the minimum bid for an interruptible rental."""
        gpu = bid if (rental_type == "bid" and bid is not None) else self.gpu_price(rental_type)
        return gpu + self.disk_hourly(disk_gb)


@dataclass(frozen=True)
class SearchQuota:
    """Vast's daily quota of search rows (staging, 2026-10-11: 20,000 a day for the account, reset at 00:00 UTC),
    from the `x-ratelimit-*` headers of a search answer. Once `remaining` reaches 0, every search answers 429
    `search_quota_exceeded` until `reset_at`, so Krater can't find offers and rents nothing."""

    limit: int
    remaining: int
    reset_at: datetime

    def exhausted(self, now: datetime) -> bool:
        return self.remaining <= 0 and now < self.reset_at

    def fraction_left(self, now: datetime) -> float:
        """What's left of the quota, 1.0 once the reset has passed."""
        if now >= self.reset_at or self.limit <= 0:
            return 1.0
        return max(self.remaining, 0) / self.limit


@dataclass(frozen=True)
class OfferQuery:
    """A search for offers. Turned into Vast's filter body by `to_body`; sorted by `dph_total` ascending
    server-side, which (at the job's own `disk_gb`) is the same order as `Offer.full_hourly` for on-demand."""

    rental_type: RentalType = "bid"
    gpu_names: tuple[str, ...] = ()
    min_gpu_ram_gb: float | None = None
    num_gpus: int = 1
    disk_gb: int = 16
    min_reliability: float = 0.95
    min_cuda: float | None = None
    min_direct_ports: int = 0
    machine_id: str | None = None
    verified: bool = True
    limit: int = SEARCH_RESULT_CAP
    #: Only offers that could cost this much or less an hour, in micro-dollars: `min_bid` at or under it for a bid
    #: (the GPU alone, so a necessary condition), `dph_total` (GPU plus this query's disk) on demand. A waiting job's
    #: search then returns no rows at all, and rows are what Vast's daily search quota counts.
    max_price_microusd: int | None = None

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "limit": min(self.limit, SEARCH_RESULT_CAP),
            "type": self.rental_type,
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "num_gpus": {"eq": self.num_gpus},
            "reliability": {"gte": self.min_reliability},
            "disk_space": {"gte": self.disk_gb},
            # Prices `dph_total` at the job's real disk, not Vast's ~8 GB default.
            "allocated_storage": self.disk_gb,
            "order": [["dph_total", "asc"]],
        }
        if self.verified:
            body["verified"] = {"eq": True}
        if len(self.gpu_names) == 1:
            body["gpu_name"] = {"eq": self.gpu_names[0]}
        elif self.gpu_names:
            body["gpu_name"] = {"in": list(self.gpu_names)}
        if self.min_gpu_ram_gb is not None:
            # In MB, like offers report it: Vast's own CLI multiplies a `gpu_ram>=24` query by 1000 before sending.
            # Not yet checked against the live API.
            body["gpu_ram"] = {"gte": self.min_gpu_ram_gb * 1000}
        if self.min_cuda is not None:
            body["cuda_max_good"] = {"gte": self.min_cuda}
        if self.min_direct_ports > 0:
            body["direct_port_count"] = {"gte": self.min_direct_ports}
        if self.machine_id is not None:
            body["machine_id"] = {"eq": vast_id(self.machine_id)}
        if self.max_price_microusd is not None:
            price = float(microusd_to_usd(self.max_price_microusd))
            body["min_bid" if self.rental_type == "bid" else "dph_total"] = {"lte": price}
        return body


@dataclass(frozen=True)
class CreateInstance:
    """What to start on an offer (`PUT /api/v0/asks/{offer}/`). `bid_price` set means an interruptible
    rental at that bid; `None` means on demand.

    `env` and `ports` become Vast's `env` string of Docker flags. The runner gets its start through `onstart`
    (a shell script, at most `ONSTART_MAX_CHARS`) or `args` (the image entrypoint's arguments).
    """

    image: str
    disk_gb: int
    label: str
    bid_price: Decimal | None = None
    env: dict[str, str] = field(default_factory=dict)
    ports: tuple[int, ...] = ()
    onstart: str | None = None
    args: tuple[str, ...] | None = None
    runtype: str | None = None

    def __post_init__(self) -> None:
        if self.onstart is not None and len(self.onstart) > ONSTART_MAX_CHARS:
            raise ValueError(f"onstart is {len(self.onstart)} characters; Vast allows {ONSTART_MAX_CHARS}")
        for key in self.env:
            if not key.replace("_", "").isalnum() or key[0].isdigit():
                raise ValueError(f"not a valid environment variable name: {key!r}")

    def env_string(self) -> str:
        flags = [f"-e {shlex.quote(f'{key}={value}')}" for key, value in self.env.items()]
        flags += [f"-p {port}:{port}" for port in self.ports]
        return " ".join(flags)

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"image": self.image, "disk": self.disk_gb, "label": self.label}
        if self.bid_price is not None:
            body["price"] = float(self.bid_price)
        env = self.env_string()
        if env:
            body["env"] = env
        if self.onstart is not None:
            body["onstart"] = self.onstart
        if self.args is not None:
            body["args"] = list(self.args)
        if self.runtype is not None:
            body["runtype"] = self.runtype
        return body

    def __repr__(self) -> str:
        # `env` carries member secrets (an HF token); never put their values in a repr or a log line.
        return (
            f"CreateInstance(image={self.image!r}, disk_gb={self.disk_gb}, label={self.label!r}, "
            f"bid_price={self.bid_price}, env_keys={sorted(self.env)}, ports={self.ports})"
        )


@dataclass(frozen=True)
class Instance:
    """One of the account's instances, from `GET /api/v1/instances/` or `GET /api/v0/instances/{id}/`.

    `actual_status` is `None` while the image pulls, `running` once the container runs (the GPU is billed only
    from then), and something else when stopped, paused (an outbid interruptible) or exited. `gpu_util` lags a
    real GPU burst by 50-105 s and flickers, so it's only a fallback signal for idleness.
    """

    id: str
    label: str | None
    actual_status: str | None
    intended_status: str | None
    cur_state: str | None
    next_state: str | None
    status_msg: str | None
    gpu_util: float | None
    cpu_util: float | None
    dph_total: Decimal | None
    dph_base: Decimal | None
    min_bid: Decimal | None
    is_bid: bool
    machine_id: str | None
    gpu_name: str | None
    num_gpus: int | None
    disk_space: float | None
    start_date: datetime | None
    ssh_host: str | None
    ssh_port: int | None
    public_ipaddr: str | None
    ports: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Instance:
        return cls(
            id=str(int(raw["id"])),
            label=raw.get("label"),
            actual_status=raw.get("actual_status"),
            intended_status=raw.get("intended_status"),
            cur_state=raw.get("cur_state"),
            next_state=raw.get("next_state"),
            status_msg=raw.get("status_msg"),
            gpu_util=_float(raw.get("gpu_util")),
            cpu_util=_float(raw.get("cpu_util")),
            dph_total=dec(raw.get("dph_total")),
            dph_base=dec(raw.get("dph_base")),
            min_bid=dec(raw.get("min_bid")),
            is_bid=bool(raw.get("is_bid")),
            machine_id=_id(raw.get("machine_id")),
            gpu_name=raw.get("gpu_name"),
            num_gpus=_int(raw.get("num_gpus")),
            disk_space=_float(raw.get("disk_space")),
            start_date=_timestamp(raw.get("start_date")),
            ssh_host=raw.get("ssh_host"),
            ssh_port=_int(raw.get("ssh_port")),
            public_ipaddr=raw.get("public_ipaddr"),
            ports=raw.get("ports") or {},
        )

    @property
    def is_running(self) -> bool:
        return self.actual_status == "running"

    @property
    def status(self) -> str | None:
        """Vast's own word for the machine's state, for display and the instance row (`last_provider_status`)."""
        return self.actual_status

    @property
    def phase(self) -> Phase:
        """The neutral phase. `intended_status` `stopped` covers Krater's stop, an outbid machine and a runner's
        self-stop alike (Vast doesn't tell them apart). Vast never reports an ended machine, only a missing one, so
        nothing here is `GONE`."""
        if self.actual_status == "running":
            return Phase.RUNNING
        if self.intended_status == "stopped":
            return Phase.STOPPED
        if self.actual_status in (None, "loading", "created"):
            return Phase.PROVISIONING
        return Phase.INTERRUPTED

    @property
    def stopping(self) -> bool:
        """Running but no longer meant to: Vast keeps `actual_status` at `running` for a while after a stop, with
        neither its intended nor its current state `running` any more."""
        return "running" not in (self.intended_status, self.cur_state)


@dataclass(frozen=True)
class ChargeItem:
    type: str  # gpu, disk, bwd (download), bwu (upload)
    amount: Decimal  # dollars, rounded by Vast to $0.001
    description: str | None


@dataclass(frozen=True)
class Charge:
    """One contract's charges from `GET /api/v0/charges/`. A row can span several days, and Vast rounds every
    amount to $0.001, so reconcile on an instance's cumulative total, never on sums of rounded daily rows."""

    source: str
    type: str
    start: datetime | None
    end: datetime | None
    amount: Decimal
    items: tuple[ChargeItem, ...]
    label: str | None

    @property
    def instance_id(self) -> str | None:
        """The Vast instance id, for a `source` of `instance-<id>`."""
        prefix = "instance-"
        number = self.source[len(prefix) :]
        if self.source.startswith(prefix) and number.isascii() and number.isdigit():
            return str(int(number))
        return None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Charge:
        metadata = raw.get("metadata") or {}
        return cls(
            source=str(raw.get("source") or ""),
            type=str(raw.get("type") or ""),
            start=_timestamp(raw.get("start")),
            end=_timestamp(raw.get("end")),
            amount=dec(raw.get("amount")) or Decimal(0),
            items=tuple(
                ChargeItem(
                    type=str(item.get("type") or ""),
                    amount=dec(item.get("amount")) or Decimal(0),
                    description=item.get("description"),
                )
                for item in raw.get("items") or ()
            ),
            label=metadata.get("label") if isinstance(metadata, dict) else None,
        )
