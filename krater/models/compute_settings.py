"""Krater Compute's policy numbers: global defaults (`ComputeSettings`, one row) and per-project overrides
(`ProjectComputeSettings`, every column nullable: `None` means "use the global default").

Admins edit both on `/admin/compute`; `krater.services.compute_settings` owns the defaults, the bounds and the
resolution of a project's effective policy. Every change writes an `AuditEvent`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base

#: The one `ComputeSettings` row's id.
GLOBAL_ID = 1


class ComputeSettings(Base):
    """The global defaults. Exactly one row (`id = 1`), seeded by the migration."""

    __tablename__ = "compute_settings"
    __table_args__ = (sa.CheckConstraint(f"id = {GLOBAL_ID}", name="singleton"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, default=GLOBAL_ID)
    # Minimum Vast host reliability, in basis points (9500 = 0.95).
    reliability_floor_bp: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Krater bids an offer's min_bid plus this percent, never above the job's max price.
    bid_margin_percent: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # A paused (outbid) job waits this long before Krater raises its bid...
    pause_grace_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # ...and is moved to another machine if still paused after this long.
    move_after_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # A box with no GPU/CPU use and no open session for this long is stopped...
    box_idle_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # ...and destroyed after being stopped this long.
    box_destroy_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Admins are alerted when Vast's hourly charges differ from Krater's estimate by more than this percent.
    reconcile_alert_percent: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # A running machine whose runner hasn't started within this long is destroyed (the image can't run it)...
    runner_start_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # ...and one whose runner stops sending heartbeats for this long.
    heartbeat_timeout_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # A runner that can't reach Krater for this long stops its own machine (keeping its disk).
    runner_self_stop_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Checkpoints and outputs a project's machines may keep in Krater's storage, in GB.
    storage_quota_gb: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    box_sync_deadline_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    box_resume_timeout_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Krater rents no host charging more than this per GB of data into or out of the machine (each way), in
    # micro-dollars per GB (20000 = $0.02/GB = $20 per TB). Vast bills transfer per GB on top of the hourly cost.
    transfer_cap_microusd_per_gb: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Members are warned when a job's transfer could cost (their estimate at the cap) or has cost (Vast's charges)
    # this percent of the project's budget.
    transfer_warn_percent: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The most machines one project may have running (or starting, or outbid) at once. A stopped box doesn't count.
    max_machines: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="4")
    # A batch job whose GPU stays nearly unused this long gets one warning in its project's channel. 0 = never.
    job_idle_warn_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="30")

    # -- Organization-wide (no per-project override) --
    # Krater's spend across every project in a UTC day / calendar month (UTC), past which the scheduler stops all
    # compute until the period rolls over or an admin raises the cap. 0 = no cap. Off by default.
    org_daily_cap_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    org_monthly_cap_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # Admins are alerted when the provider account's credit falls under this. 0 = no alert. Off by default.
    credit_alert_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # A finished (completed or withdrawn) project's checkpoints and outputs are deleted this many days after it
    # finished; the date is fixed when it finishes (`Project.files_delete_on`). 0 = kept.
    finished_files_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="30")
    # Krater rents nothing new while the provider account's credit wouldn't cover this many hours of every machine
    # it holds plus the new one. 0 = off (the default: the credit reading isn't confirmed against Vast yet).
    credit_reserve_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")

    # The stop switch: set while an admin has stopped all compute (`krater.services.compute_halt`).
    paused_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    paused_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True)
    # Written by the scheduler: since when compute has been stopped (by the switch or a spending cap), and why, as
    # members read it. Null while it runs. When it clears, waiting jobs get that time back on their max wait.
    halted_since: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    halt_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )


class ProjectComputeSettings(Base):
    """One project's overrides. A row exists only once an admin has overridden something; a `None` column
    means that number follows the global default."""

    __tablename__ = "project_compute_settings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    reliability_floor_bp: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    bid_margin_percent: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    pause_grace_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    move_after_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    box_idle_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    box_destroy_hours: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    reconcile_alert_percent: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    runner_start_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    heartbeat_timeout_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    runner_self_stop_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    storage_quota_gb: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    box_sync_deadline_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    box_resume_timeout_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    transfer_cap_microusd_per_gb: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    transfer_warn_percent: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    max_machines: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    job_idle_warn_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )
