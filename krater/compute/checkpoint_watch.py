"""Warning a job's team when its checkpoints have stopped reaching storage.

A job on a bid machine can be outbid and moved at any time, and then carries on from its last stored checkpoint. A
job that never writes to `$KRATER_CHECKPOINT_DIR`, or stopped writing, would start over: the job page marks that
"overdue" (`compute_files.checkpoint_freshness`), and this tells the project's channel once per attempt, mentioning
whoever submitted it. Run every 15 minutes by the worker, since it lists storage for each running job. Smoke tests
and on-demand machines (which can't be outbid) are left out. Framework-free.
"""

from __future__ import annotations

import logging
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeEvent, ComputeJob
from krater.models.enums import ComputeJobKind, ComputeJobState
from krater.services import audit, compute_files, compute_jobs
from krater.storage import ObjectStore, StorageError

logger = logging.getLogger(__name__)

EVENT = "checkpoint_overdue"


def _warned(session: Session, job: ComputeJob) -> bool:
    payloads = session.scalars(
        sa.select(ComputeEvent.payload).where(ComputeEvent.job_id == job.id, ComputeEvent.kind == EVENT)
    ).all()
    return any((payload or {}).get("attempt") == job.attempts for payload in payloads)


def warn_overdue(session: Session, store: ObjectStore, *, now: datetime) -> int:
    """Warn, once per attempt, about each running job whose checkpoints are overdue. Returns how many. Flushes."""
    jobs = session.scalars(
        sa.select(ComputeJob).where(
            ComputeJob.kind == ComputeJobKind.JOB,
            ComputeJob.state == ComputeJobState.RUNNING,
            ComputeJob.interruptible.is_(True),
            sa.or_(ComputeJob.smoke_minutes.is_(None), ComputeJob.smoke_minutes == 0),
        )
    ).all()
    warned = 0
    for job in jobs:
        live = [row for row in job.instances if row.is_live]
        running_since = live[-1].entered_running_at if live else None
        if running_since is None or _warned(session, job):
            continue
        every = job.checkpoint_upload_minutes or compute_jobs.DEFAULT_CHECKPOINT_UPLOAD_MINUTES
        prefix = f"projects/{job.project_id}/jobs/{job.id}/checkpoints/"
        try:
            files = [
                compute_files.JobFile("checkpoints", item.key[len(prefix) :], item.size_bytes, item.last_modified)
                for item in store.list_prefix(prefix)
            ]
        except StorageError:
            logger.warning("krater: couldn't list %s's checkpoints; trying again next run", job.id)
            continue
        fresh = compute_files.checkpoint_freshness(files, every_minutes=every, running_since=running_since, now=now)
        if not fresh["overdue"]:
            continue
        message = (
            f"No checkpoint stored for {fresh['minutes_ago']} minutes"
            if fresh["newest"] is not None
            else "No checkpoint stored yet"
        ) + (
            f" (it uploads every {every} minutes). If the machine is outbid and the job moves, it starts again from "
            "its last stored checkpoint: save them to $KRATER_CHECKPOINT_DIR."
        )
        session.add(ComputeEvent(job_id=job.id, kind=EVENT, message=message, payload={"attempt": job.attempts}, at=now))
        audit.record(
            session,
            None,
            compute_jobs.AUDIT_CHECKPOINT_OVERDUE,
            project=job.project,
            payload={
                "job_id": str(job.id),
                "job_name": job.name,
                "owner_id": str(job.submitter_id),
                "minutes_ago": fresh["minutes_ago"],
                "every_minutes": every,
            },
        )
        warned += 1
    session.flush()
    return warned
