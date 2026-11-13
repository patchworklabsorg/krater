"""What a box's member is told about its machine: what it costs right now, when /workspace was last saved, and when
a stopped box is destroyed. The job page, the API (and so `krater show`) and the Slack notices all say the same.

A stopped box keeps its machine's disk, which still bills (at the disk part of its price) until it is resumed or
destroyed. Krater destroys it after the project's `box_destroy_hours`. Only /workspace is saved to Krater, every
`WORKSPACE_SAVE_MINUTES` while the box runs and before Krater stops it, and only onto the same box's next machine:
once the box is destroyed (cancelled, or stopped too long) its member can't restore or download it."""

from __future__ import annotations

from datetime import datetime, timedelta

from krater.compute.runner_api import CHECKPOINT_SYNC_SECONDS
from krater.compute.spend import MICROUSD_PER_CENT, ceil_div, hourly_rate_microusd
from krater.models import ComputeInstance, ComputeJob
from krater.models.compute_job import ComputeInstanceState

#: How often a running box's runner saves /workspace (it gets the checkpoint sync interval).
WORKSPACE_SAVE_MINUTES = CHECKPOINT_SYNC_SECONDS // 60

__all__ = ["WORKSPACE_SAVE_MINUTES", "destroy_at", "hourly_rate_microusd", "last_saved", "per_day_cents"]


def destroy_at(instance: ComputeInstance | None, destroy_hours: int) -> datetime | None:
    """When Krater destroys a stopped box's machine (on its first pass after then), or None if it isn't stopped."""
    if instance is None or instance.state is not ComputeInstanceState.STOPPED or instance.stopped_at is None:
        return None
    return instance.stopped_at + timedelta(hours=destroy_hours)


def last_saved(job: ComputeJob) -> datetime | None:
    """The box's last confirmed save of /workspace, on any of its machines (a move starts from the previous one's)."""
    return max((row.last_synced_at for row in job.instances if row.last_synced_at is not None), default=None)


def per_day_cents(microusd_per_hour: int) -> int:
    """An hourly rate as cents a day, rounded up."""
    return ceil_div(microusd_per_hour * 24, MICROUSD_PER_CENT)
