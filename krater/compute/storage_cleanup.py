"""Abort multipart uploads that machines started and never finished: their parts take space on Krater's storage.
Also delete old `--code` folders no unfinished job uses (`prune_code_inputs`).

SeaweedFS (4.47) doesn't say when an upload started, so age alone can't decide. An upload is abandoned once its job
has ended (with a grace for a final upload still finishing), or its key belongs to no job Krater knows. Where the
backend does report a start time, uploads older than a day are aborted too.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.compute import runner_api
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


#: `krater submit --code` uploads each version of a folder as the input `code/<hash>/` (`cli/src/krater_cli/code.py`).
CODE_PREFIX = "code/"
#: A code folder no unfinished job uses is deleted once its newest file is this old: long enough to `krater rerun`
#: a recent job, short enough that a project's storage doesn't fill with every edit.
CODE_KEEP = timedelta(days=7)
#: Only projects with a job this recent can have code folders worth looking at.
CODE_LOOKBACK = timedelta(days=60)


def prune_code_inputs(session: Session, store: ObjectStore, *, now: datetime) -> int:
    """Delete the `code/<hash>/` input folders that no unfinished job asks for and whose newest file is older than
    `CODE_KEEP`. Returns how many folders went. A folder whose files carry no time is kept."""
    # Every unfinished job counts, however old; recent jobs only say which projects to look at.
    jobs = session.scalars(
        sa.select(ComputeJob).where(
            sa.or_(ComputeJob.queued_at >= now - CODE_LOOKBACK, ComputeJob.state.not_in(FINISHED_JOB_STATES))
        )
    ).all()
    projects: dict[uuid.UUID, set[str]] = {}
    for job in jobs:
        code = {name for name in job.input_names or () if name.startswith(CODE_PREFIX)}
        if not code:
            continue
        used = projects.setdefault(job.project_id, set())
        if job.state not in FINISHED_JOB_STATES:
            used.update(name.rstrip("/") + "/" for name in code)
    gone = 0
    for project_id, used in projects.items():
        prefix = runner_api.inputs_prefix(project_id)
        folders: dict[str, list] = {}
        for item in store.list_prefix(prefix + CODE_PREFIX):
            rest = item.key[len(prefix) :]
            parts = rest.split("/")
            if len(parts) < 3:
                continue
            folders.setdefault(f"{parts[0]}/{parts[1]}/", []).append(item)
        for folder, items in folders.items():
            times = [i.last_modified for i in items]
            if folder in used or any(t is None for t in times) or now - max(times) < CODE_KEEP:
                continue
            for item in items:
                store.delete(item.key)
            gone += 1
            logger.info(
                "krater.compute: deleted code folder %s of project %s (unused, %s old)",
                folder,
                project_id,
                now - max(times),
            )
    return gone
