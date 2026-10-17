"""Keep admins informed about Vast's daily search quota (docs/compute/vast-api.md).

Only the worker talks to Vast, so after each scheduler pass and price-history run it saves the quota its client last
saw into `VastSearchQuota`, which `/admin/compute` shows. When under a quarter is left, and when it's used up, it
writes an `AuditEvent` and logs an error, once per day (per reset): with the quota gone Krater can't start any job.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from krater.compute.search_pacing import LOW_QUOTA_FRACTION
from krater.models import VastSearchQuota
from krater.models.vast_search_quota import QUOTA_ID
from krater.services import audit
from krater.vast import SearchQuota

logger = logging.getLogger(__name__)

AUDIT_QUOTA_LOW = "compute_search_quota_low"
AUDIT_QUOTA_EXHAUSTED = "compute_search_quota_exhausted"


def latest(session: Session) -> VastSearchQuota | None:
    return session.get(VastSearchQuota, QUOTA_ID)


def record(session: Session, quota: SearchQuota | None, *, now: datetime) -> None:
    """Save `quota` (the client's latest; `None` before its first search) and write any alert due. Flushes."""
    if quota is None:
        return
    row = latest(session)
    if row is None:
        row = VastSearchQuota(id=QUOTA_ID, limit_rows=quota.limit, remaining_rows=quota.remaining)
        session.add(row)
    row.limit_rows = quota.limit
    row.remaining_rows = quota.remaining
    row.reset_at = quota.reset_at
    row.observed_at = now
    payload = {"remaining": quota.remaining, "limit": quota.limit, "reset_at": quota.reset_at.isoformat()}
    if quota.exhausted(now):
        if row.exhausted_alerted_reset_at != quota.reset_at:
            row.exhausted_alerted_reset_at = quota.reset_at
            logger.error(
                "krater.compute: Vast's daily search quota is used up until %s; no job can start until then",
                f"{quota.reset_at:%H:%M} UTC",
            )
            audit.record(session, None, AUDIT_QUOTA_EXHAUSTED, payload=payload)
    elif quota.fraction_left(now) < LOW_QUOTA_FRACTION and row.low_alerted_reset_at != quota.reset_at:
        row.low_alerted_reset_at = quota.reset_at
        logger.error(
            "krater.compute: %d of %d Vast search rows left until %s; searches slow down",
            quota.remaining,
            quota.limit,
            f"{quota.reset_at:%H:%M} UTC",
        )
        audit.record(session, None, AUDIT_QUOTA_LOW, payload=payload)
    session.flush()
