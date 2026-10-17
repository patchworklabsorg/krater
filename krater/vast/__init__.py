"""`krater.vast`: the only place that talks to Vast.ai.

Nothing outside this package imports `httpx` for Vast or knows its URLs or wire shapes. Only Krater's worker calls
`get_vast_client`: the organization's Vast key never reaches the portal or a member's machine.
See docs/compute/vast-api.md for the facts the client follows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from krater.config import Settings, get_settings
from krater.vast.client import VastClient
from krater.vast.errors import (
    VastError,
    VastRateLimitedError,
    VastRedirectError,
    VastRequestFailedError,
    VastSearchQuotaError,
    VastUnavailableError,
)
from krater.vast.fake import FakeVast
from krater.vast.types import (
    Charge,
    ChargeItem,
    CreateInstance,
    Instance,
    Offer,
    OfferQuery,
    RentalType,
    SearchQuota,
    microusd_to_usd,
    usd_to_microusd,
    vast_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Charge",
    "ChargeItem",
    "CreateInstance",
    "FakeVast",
    "Instance",
    "Offer",
    "OfferQuery",
    "RentalType",
    "SearchQuota",
    "VastClient",
    "VastError",
    "VastRateLimitedError",
    "VastRedirectError",
    "VastRequestFailedError",
    "VastSearchQuotaError",
    "VastUnavailableError",
    "build_vast_client",
    "get_vast_client",
    "microusd_to_usd",
    "usd_to_microusd",
    "vast_id",
]


def _read_api_key(settings: Settings) -> str:
    if settings.vast_api_key_file:
        try:
            return Path(settings.vast_api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("krater.vast: could not read KRATER_VAST_API_KEY_FILE")
            return ""
    return settings.vast_api_key.strip()


def build_vast_client(settings: Settings) -> VastClient | None:
    """A client for `settings`, or `None` when Krater has no usable Vast account: live mode without a key, or the
    fake in production (fake market prices must never land in production's tables)."""
    if settings.vast_mode == "fake":
        if settings.env == "production":
            return None
        return FakeVast().client()
    api_key = _read_api_key(settings)
    if not api_key:
        return None
    return VastClient(api_key, base_url=settings.vast_api_url, timeout_seconds=settings.vast_timeout_seconds)


#: The worker's client and the key it was built with. Not `lru_cache`: a missing key must not be cached, and a
#: rotated key file must take effect without restarting the worker.
_cached: tuple[str, VastClient] | None = None


def get_vast_client() -> VastClient | None:
    """The worker's client (see `build_vast_client`). The fake is built once, so it keeps its state in dev; a live
    client is rebuilt whenever the key changes. Live mode without a key logs an error on every call."""
    global _cached
    settings = get_settings()
    if settings.vast_mode == "fake":
        if _cached is None or _cached[0] != "fake":
            client = build_vast_client(settings)
            if client is None:
                return None
            _cached = ("fake", client)
        return _cached[1]
    api_key = _read_api_key(settings)
    if not api_key:
        logger.error("krater.vast: KRATER_VAST_MODE=live but no Vast API key is readable; nothing will be rented")
        _cached = None
        return None
    if _cached is None or _cached[0] != api_key:
        _cached = (
            api_key,
            VastClient(api_key, base_url=settings.vast_api_url, timeout_seconds=settings.vast_timeout_seconds),
        )
    return _cached[1]
