"""The public GPU price catalog behind `/pricing` and the budget estimator: SkyPilot's Vast catalog CSV, fetched
straight from GitHub (no SkyPilot server, no credentials). See docs/dev/pricing.md.

It outlives Krater's SkyPilot integration because proposals are budgeted before any machine is rented, and production
runs with compute off (so without Krater's own Vast price history) until Krater Compute is turned on. `fake` serves a
canned catalog for dev and tests; production refuses it.
"""

from __future__ import annotations

import ast
import csv
import io
from dataclasses import dataclass
from functools import lru_cache

import httpx

from krater.config import get_settings


class CatalogError(Exception):
    """The catalog couldn't be fetched or parsed. The refresh fails soft: the last prices stay."""


@dataclass(frozen=True)
class GpuOffer:
    """One catalog row: one accelerator/count offering in one region. `device_memory_gib` is the GPU's VRAM;
    `memory_gib` the host's RAM. `spot_price_dollars` of 0.0 means "no spot price quoted"."""

    accelerator_name: str
    accelerator_count: int
    vcpus: float | None
    memory_gib: float | None
    device_memory_gib: float | None
    price_dollars: float
    spot_price_dollars: float
    region: str


def _parse_float(value: str | None, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_device_memory_gib(gpu_info_raw: str) -> float | None:
    """VRAM in GiB from the catalog's `GpuInfo` column, a Python-dict-repr string (hence `ast.literal_eval`): the first
    GPU's `MemoryInfo.SizeInMiB` / 1024, the same arithmetic `sky show-gpus` uses. `None` if blank or unparseable."""
    if not gpu_info_raw:
        return None
    try:
        parsed = ast.literal_eval(gpu_info_raw)
        size_mib = parsed["Gpus"][0]["MemoryInfo"]["SizeInMiB"]
    except (ValueError, SyntaxError, KeyError, IndexError, TypeError):
        return None
    return float(size_mib) / 1024.0


def parse_vast_catalog_csv(text: str) -> list[GpuOffer]:
    """Parse `vast/vms.csv` into `GpuOffer` rows. A row without an `AcceleratorName`, or with an unparseable count or
    price, is skipped: one malformed row shouldn't blank the pricing page."""
    offers: list[GpuOffer] = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("AcceleratorName") or "").strip()
        if not name:
            continue
        try:
            count = int(float(row["AcceleratorCount"]))
            price = float(row["Price"])
        except (KeyError, ValueError, TypeError):
            continue
        offers.append(
            GpuOffer(
                accelerator_name=name,
                accelerator_count=count,
                vcpus=_parse_optional_float(row.get("vCPUs")),
                memory_gib=_parse_optional_float(row.get("MemoryGiB")),
                device_memory_gib=_parse_device_memory_gib(row.get("GpuInfo") or ""),
                price_dollars=price,
                spot_price_dollars=_parse_float(row.get("SpotPrice")),
                region=(row.get("Region") or "").strip(),
            )
        )
    return offers


class LiveCatalog:
    """Fetches the CSV over HTTPS. `http_client` is injectable for tests."""

    def __init__(self, url: str, *, timeout_seconds: float, http_client: httpx.Client | None = None) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)

    def list_gpu_prices(self) -> list[GpuOffer]:
        try:
            response = self._http.get(self._url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise CatalogError(f"could not fetch the Vast GPU catalog: {exc}") from exc
        if response.status_code >= 400:
            raise CatalogError(f"fetching the Vast GPU catalog returned HTTP {response.status_code} from {self._url}")
        try:
            return parse_vast_catalog_csv(response.text)
        except Exception as exc:
            raise CatalogError(f"could not parse the Vast GPU catalog: {exc}") from exc


#: A small stand-in for the real catalog: several regions and counts for one GPU, a GPU with no spot price, a
#: multi-GPU count. Dev and tests only.
DEFAULT_FAKE_OFFERS: tuple[GpuOffer, ...] = (
    GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.10, 0.35, "US, NA"),
    GpuOffer("A100", 1, 16.0, 64.0, 40.0, 1.35, 0.40, "Germany, DE, EU"),
    GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.20, 0.0, "South Korea, KR, AS"),
    GpuOffer("A100", 2, 32.0, 128.0, 40.0, 2.20, 0.70, "US, NA"),
    GpuOffer("RTX4090", 1, 16.0, 32.0, 24.0, 0.35, 0.12, "US, NA"),
    GpuOffer("RTX4090", 1, 16.0, 32.0, 24.0, 0.40, 0.15, "Canada, CA, NA"),
    GpuOffer("H100", 1, 32.0, 128.0, 80.0, 2.50, 0.0, "US, NA"),
)


class FakeCatalog:
    def __init__(self) -> None:
        self._offers: list[GpuOffer] = list(DEFAULT_FAKE_OFFERS)

    def list_gpu_prices(self) -> list[GpuOffer]:
        return list(self._offers)

    def set_gpu_offers(self, offers: list[GpuOffer]) -> None:
        self._offers = list(offers)


@lru_cache
def get_price_catalog() -> LiveCatalog | FakeCatalog:
    settings = get_settings()
    if settings.pricing_mode == "fake":
        return FakeCatalog()
    return LiveCatalog(settings.pricing_catalog_url, timeout_seconds=settings.pricing_catalog_fetch_timeout_seconds)
