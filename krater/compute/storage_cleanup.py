"""Abort multipart uploads that machines started and never finished: their parts take space on Krater's storage.

SeaweedFS (4.47) doesn't say when an upload started, so age alone can't decide. An upload is abandoned once its job
has ended (with a grace for a final upload still finishing), or its key belongs to no job Krater knows. Where the
backend does report a start time, uploads older than a day are aborted too.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from krater.models import ComputeJob
from krater.models.compute_job import FINISHED_JOB_STATES
from krater.storage import ObjectStore

logger = logging.getLogger(__name__)

#: A job's final uploads may still be finishing for a while after it ends.
GRACE_AFTER_END = timedelta(hours=1)
MAX_AGE = timedelta(days=1)
_JOB_KEY = re.compile(r"^projects/[0-9a-f-]{36}/jobs/(?P<job>[0-9a-f-]{36})/")


def _abandoned(session: Session, key: str, initiated: datetime | None, *, now: datetime) -> bool:
    if initiated is not None and now - initiated > MAX_AGE:
        return True
    match = _JOB_KEY.match(key)
    if match is None:
        return True
    job = session.get(ComputeJob, uuid.UUID(match["job"]))
    if job is None:
        return True
    return job.state in FINISHED_JOB_STATES and job.ended_at is not None and now - job.ended_at > GRACE_AFTER_END


def abort_abandoned_uploads(session: Session, store: ObjectStore, *, now: datetime) -> int:
    aborted = 0
    for upload in store.list_multipart_uploads("projects/"):
        if _abandoned(session, upload.key, upload.initiated, now=now):
            store.abort_multipart_upload(upload.key, upload_id=upload.upload_id)
            aborted += 1
    return aborted
