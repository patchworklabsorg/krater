"""What the scheduler learns about images and the market as it rents: image sizes (for pricing the pull), and the
data that wait-time forecasts and outbid-adjusted prices will need later (`krater.models.compute_market`).

Collecting only: nothing here changes what Krater rents, except that a known image size enters the ranking and the
budget held (`krater.compute.offers.Transfer`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from krater.models import ComputeJob, ComputeWaitSample, ImageSize
from krater.registry import Registry, RegistryError, parse_reference

logger = logging.getLogger(__name__)

#: How long a failed size lookup is trusted before asking the registry again.
RETRY_FAILED_AFTER = timedelta(hours=1)


def image_bytes(
    session: Session, registry: Registry | None, image: str, digest: str | None, *, now: datetime
) -> int | None:
    """The compressed size of `image` at `digest`, from Krater's cache or, once per digest, the registry. `None` when
    it isn't known (no digest, or the registry couldn't say): the pull is then left out of the estimate, as before."""
    if registry is None or digest is None:
        return None
    row = session.get(ImageSize, digest)
    if row is not None and (row.size_bytes is not None or now - row.checked_at < RETRY_FAILED_AFTER):
        return row.size_bytes
    if row is None:
        row = ImageSize(digest=digest, checked_at=now)
        session.add(row)
    row.checked_at = now
    try:
        row.size_bytes = registry.image_size(parse_reference(image).pinned(digest))
        row.error = None
    except RegistryError as exc:
        logger.warning("krater.compute: no size for %s: %s", image, exc)
        row.size_bytes = None
        row.error = str(exc)[:300]
    session.flush()
    return row.size_bytes


def record_wait(
    session: Session,
    job: ComputeJob,
    *,
    now: datetime,
    outcome: str,
    rental_type: str,
    limit_microusd: int | None,
    rented_microusd: int | None = None,
    cheapest_microusd: int | None = None,
) -> None:
    """One wait for a machine, from when `job` started waiting (by the wall clock, halts included) to `now`."""
    started = job.wait_started_at or job.queued_at
    session.add(
        ComputeWaitSample(
            job_id=job.id,
            attempt=job.attempts + (1 if outcome == "rented" else 0),
            gpu_names=list(job.gpu_names or []),
            num_gpus=job.num_gpus,
            min_gpu_ram_gb=job.min_gpu_ram_gb,
            rental_type=rental_type,
            target_microusd=job.target_microusd,
            max_microusd=job.max_microusd,
            limit_microusd=limit_microusd,
            started_at=started,
            ended_at=now,
            waited_seconds=max(0, int((now - started).total_seconds())),
            outcome=outcome,
            rented_microusd=rented_microusd,
            cheapest_microusd=cheapest_microusd,
        )
    )
