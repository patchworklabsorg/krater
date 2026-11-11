"""Krater Compute's live spend estimate.

Every scheduler pass, each instance that may be billing accrues its rented price for the time since the last pass:
disk from the moment it was created (the image pull bills disk only), and the GPU only while it was `running`
(spike, 2026-10-11: 0.194 h of GPU against 0.252 h of disk). A job's spend is the sum of its instances; a project's
spend is the sum of its jobs, written as a `SpendSnapshot` (`vast_estimate`) whenever it grows, so the project page,
the budget alerts and Quilt's `spend.recorded` events keep working as before.

Data transfer, which Vast bills per GB, comes in two parts: the image pull, priced once when the provider has the
machine (its compressed size at the host's price for data in), and what the container moves, which its runner
reports with each heartbeat (`krater.compute.runner_api`). Each pass prices the bytes reported since the last one
at the host's prices and adds them. Only increases are priced, never the running total, so after reconciliation
replaces an estimate with Vast's figure (which covers the data up to then), nothing is counted twice.

Each part is also kept on its own (`est_*_microusd`), for the breakdown against Vast's itemized charges. The estimate
only ever grows here; only reconciliation lowers a total (from a finished machine's final charges), and Quilt gets
that as `spend.corrected`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.compute.offers import bytes_cost_microusd
from krater.models import (
    ComputeInstance,
    ComputeInstanceState,
    ComputeJob,
    Project,
    SpendSnapshot,
    SpendSource,
)
from krater.services import budget, quilt_events

MICROUSD_PER_CENT = 10_000
SECONDS_PER_HOUR = 3600

#: Instance states that may be billing.
BILLING_STATES = (
    ComputeInstanceState.CREATING,
    ComputeInstanceState.PROVISIONING,
    ComputeInstanceState.RUNNING,
    ComputeInstanceState.PAUSED,
    ComputeInstanceState.STOPPED,
)


def ceil_div(numerator: int, denominator: int) -> int:
    """`numerator / denominator` rounded up, in integers: money never goes through a float."""
    return -(-numerator // denominator)


def hourly_rate_microusd(instance: ComputeInstance) -> int:
    """What the instance costs per hour in its current state: disk always, the GPU only while running."""
    if instance.state is ComputeInstanceState.RUNNING:
        return instance.disk_microusd + instance.gpu_microusd
    return instance.disk_microusd


def accrue(session: Session, *, now: datetime) -> set[uuid.UUID]:
    """Add each billing instance's cost since it last accrued, at the rate of the state it was in. Call before the
    pass changes any instance's state. Returns the ids of the projects whose spend may have changed."""
    rows = session.scalars(
        sa.select(ComputeInstance).where(
            sa.or_(
                ComputeInstance.state.in_(BILLING_STATES),
                ComputeInstance.net_in_bytes > ComputeInstance.net_priced_in_bytes,
                ComputeInstance.net_out_bytes > ComputeInstance.net_priced_out_bytes,
            )
        )
    )
    touched_jobs: set[uuid.UUID] = set()
    for row in rows:
        if _accrue_image(row) | _accrue_bytes(row):
            touched_jobs.add(row.job_id)
        if row.state not in BILLING_STATES:
            continue
        start = row.accrued_until or row.created_at
        if now <= start:
            continue
        seconds = (now - start).total_seconds()
        total = round(hourly_rate_microusd(row) * seconds / SECONDS_PER_HOUR)
        gpu = round(row.gpu_microusd * seconds / SECONDS_PER_HOUR) if row.state is ComputeInstanceState.RUNNING else 0
        row.accrued_microusd += total
        row.est_gpu_microusd += min(gpu, total)
        row.est_disk_microusd += total - min(gpu, total)
        if row.state is ComputeInstanceState.RUNNING:
            row.run_seconds += round(seconds)
        row.accrued_until = now
        touched_jobs.add(row.job_id)
    if not touched_jobs:
        return set()

    totals = session.execute(
        sa.select(ComputeInstance.job_id, sa.func.sum(ComputeInstance.accrued_microusd))
        .where(ComputeInstance.job_id.in_(touched_jobs))
        .group_by(ComputeInstance.job_id)
    )
    projects: set[uuid.UUID] = set()
    for job_id, total in totals:
        job = session.get(ComputeJob, job_id)
        job.spend_cents = ceil_div(int(total or 0), MICROUSD_PER_CENT)
        projects.add(job.project_id)
    session.flush()
    return projects


#: States in which the provider has the machine, so its host is pulling (or has pulled) the image.
PULLED_STATES = (
    ComputeInstanceState.PROVISIONING,
    ComputeInstanceState.RUNNING,
    ComputeInstanceState.PAUSED,
    ComputeInstanceState.STOPPED,
)


def _accrue_image(row: ComputeInstance) -> bool:
    """Price the image pull once the provider has the machine (not at the create, which may never happen)."""
    if row.est_image_microusd or not row.image_bytes or row.state not in PULLED_STATES:
        return False
    cost = bytes_cost_microusd(row.image_bytes, row.transfer_in_microusd_per_gb)
    if cost <= 0:
        return False
    row.est_image_microusd = cost
    row.accrued_microusd += cost
    return True


def _accrue_bytes(row: ComputeInstance) -> bool:
    """Price the bytes the runner reported since the last pass. Returns whether anything was added."""
    added = False
    new_in = row.net_in_bytes - row.net_priced_in_bytes
    if new_in > 0:
        cost = bytes_cost_microusd(new_in, row.transfer_in_microusd_per_gb)
        row.est_transfer_in_microusd += cost
        row.accrued_microusd += cost
        row.net_priced_in_bytes = row.net_in_bytes
        added = True
    new_out = row.net_out_bytes - row.net_priced_out_bytes
    if new_out > 0:
        cost = bytes_cost_microusd(new_out, row.transfer_out_microusd_per_gb)
        row.est_transfer_out_microusd += cost
        row.accrued_microusd += cost
        row.net_priced_out_bytes = row.net_out_bytes
        added = True
    return added


def project_compute_spend_cents(session: Session, project: Project) -> int:
    """The project's compute spend, rounded up once: summing each job's rounded-up cents would add up to a cent per
    job (staging, 2026-10-11: 6 cents for $0.019 of charges across seven short jobs)."""
    total = session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(ComputeInstance.accrued_microusd), 0))
        .join(ComputeJob, ComputeJob.id == ComputeInstance.job_id)
        .where(ComputeJob.project_id == project.id)
    )
    return ceil_div(int(total or 0), MICROUSD_PER_CENT)


def record_snapshots(
    session: Session, project_ids: set[uuid.UUID], *, now: datetime, allow_lower: bool = False
) -> None:
    """Write a `vast_estimate` snapshot for each project whose compute spend changed from its latest snapshot, and
    queue the matching Quilt event. Only a rise, unless `allow_lower` (reconciliation, after finished machines' final
    charges came in under the estimate)."""
    for project_id in sorted(project_ids):
        project = session.get(Project, project_id)
        total = project_compute_spend_cents(session, project)
        latest = budget.latest_spend_cents(session, project)
        if total == latest or (total < latest and not allow_lower):
            continue
        session.add(
            SpendSnapshot(
                project_id=project.id, estimated_spend_cents=total, source=SpendSource.VAST_ESTIMATE, taken_at=now
            )
        )
        session.flush()
        quilt_events.sync_project(session, project)


def next_minute_cents(session: Session, project: Project) -> int:
    """What the project's billing instances will cost over the next minute, rounded up."""
    rows = session.scalars(
        sa.select(ComputeInstance)
        .join(ComputeJob, ComputeJob.id == ComputeInstance.job_id)
        .where(ComputeJob.project_id == project.id, ComputeInstance.state.in_(BILLING_STATES))
    )
    return ceil_div(sum(hourly_rate_microusd(row) for row in rows), 60 * MICROUSD_PER_CENT)
