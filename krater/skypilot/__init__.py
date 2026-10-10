"""`krater.skypilot`: the only place that talks to SkyPilot.

Nothing outside this package should import `httpx` for SkyPilot or know its URLs or wire shapes -- go
through `SkyPilotClient` (via `get_skypilot_client`). See `docs/skypilot-integration.md` for the design
and `docs/dev/skypilot-spike.md` for the verified facts it was checked against.
"""

from __future__ import annotations

from functools import lru_cache

from krater.config import get_settings
from krater.skypilot.client import SkyPilotClient
from krater.skypilot.errors import (
    SkyPilotError,
    SkyPilotRequestFailedError,
    SkyPilotUnavailableError,
    SkyPilotWorkspaceNotFoundError,
)
from krater.skypilot.fake import FakeSkyPilotClient
from krater.skypilot.live import LiveSkyPilotClient
from krater.skypilot.types import ClusterInfo, CostReportRow, GpuOffer, ManagedJobInfo

__all__ = [
    "ClusterInfo",
    "CostReportRow",
    "FakeSkyPilotClient",
    "GpuOffer",
    "LiveSkyPilotClient",
    "ManagedJobInfo",
    "SkyPilotClient",
    "SkyPilotError",
    "SkyPilotRequestFailedError",
    "SkyPilotUnavailableError",
    "SkyPilotWorkspaceNotFoundError",
    "get_skypilot_client",
]


@lru_cache
def get_skypilot_client() -> SkyPilotClient:
    """The process-wide `SkyPilotClient`, chosen by `settings.skypilot_mode`.

    Cached like `krater.weave.get_weave_client`; override in tests by passing a `FakeSkyPilotClient`
    directly to the service functions instead of going through this factory.
    """
    settings = get_settings()
    if settings.skypilot_mode == "fake":
        return FakeSkyPilotClient()
    return LiveSkyPilotClient(settings)
