"""What a box's member is told about its machine: what it costs right now, when /workspace was last saved, and when
a stopped box is destroyed. The job page, the API (and so `krater show`) and the Slack notices all say the same.

A stopped box keeps its machine's disk, which still bills (at the disk part of its price) until it is resumed or
destroyed. Krater destroys it after the project's `box_destroy_hours`. Only /workspace is saved to Krater, every
`WORKSPACE_SAVE_MINUTES` while the box runs and before Krater stops it, into the box's workspace
(`krater.services.workspaces`). Destroying the box ends its machine and disk, not its workspace: the team can continue
it in a new box, browse and download it, or delete it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from krater.compute.runner_api import CHECKPOINT_SYNC_SECONDS
from krater.compute.spend import MICROUSD_PER_CENT, ceil_div, hourly_rate_microusd
from krater.models import ComputeInstance, ComputeJob
from krater.models.compute_job import ComputeInstanceState

#: How often a running box's runner saves /workspace (it gets the checkpoint sync interval).
WORKSPACE_SAVE_MINUTES = CHECKPOINT_SYNC_SECONDS // 60

__all__ = [
    "WORKSPACE_SAVE_MINUTES",
    "SaveTrouble",
    "destroy_at",
    "hourly_rate_microusd",
    "last_saved",
    "per_day_cents",
    "save_trouble",
]


def destroy_at(instance: ComputeInstance | None, destroy_hours: int) -> datetime | None:
    """When Krater destroys a stopped box's machine (on its first pass after then), or None if it isn't stopped."""
    if instance is None or instance.state is not ComputeInstanceState.STOPPED or instance.stopped_at is None:
        return None
    return instance.stopped_at + timedelta(hours=destroy_hours)


def last_saved(job: ComputeJob) -> datetime | None:
    """The box's last save of /workspace that stored everything, on any of its machines (a move starts from the
    previous one's): a timed save its runner reported (`workspace_saved_at`), or a stop-save it confirmed
    (`last_synced_at`; runners before 2026-11-18 report only those)."""
    times = [
        moment for row in job.instances for moment in (row.workspace_saved_at, row.last_synced_at) if moment is not None
    ]
    return max(times, default=None)


@dataclass(frozen=True)
class SaveTrouble:
    """Saves of a box's /workspace that keep failing: how many in a row, the last one's reason, and when."""

    failures: int
    error: str
    at: datetime


def save_trouble(instance: ComputeInstance | None) -> SaveTrouble | None:
    """Why the box's last save didn't store everything, if it didn't (cleared by the next good save)."""
    if instance is None or not instance.save_failures or instance.last_save_error_at is None:
        return None
    return SaveTrouble(instance.save_failures, instance.last_save_error or "", instance.last_save_error_at)


def per_day_cents(microusd_per_hour: int) -> int:
    """An hourly rate as cents a day, rounded up."""
    return ceil_div(microusd_per_hour * 24, MICROUSD_PER_CENT)
