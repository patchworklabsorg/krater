"""What a job left in Krater's storage, for its team: its output log, outputs and checkpoints.

The runner uploads them through presigned URLs (`krater.compute.runner_api`), under
`projects/<project>/jobs/<job>/<area>/`: the command's output to `logs/attempt-<n>.log` every minute and when it
exits, `$KRATER_OUTPUT_DIR` to `outputs/` when it exits, `$KRATER_CHECKPOINT_DIR` to `checkpoints/` every 10 minutes
and when it exits. This module lists them, shows the end of a log, and hands out short-lived download URLs.

Only the project's team and admins may read them: outputs and logs can hold anything the job printed or wrote.
Framework-free; the job page, the API and the `krater` CLI call it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from krater.models import ComputeJob, Project
from krater.services import teams
from krater.services.actor import Actor
from krater.services.errors import NotAllowed, NotFound
from krater.storage import ObjectStore

#: The areas a member can read. A box's `workspace` is restored onto its next machine instead.
AREAS = ("outputs", "checkpoints", "logs")
#: How much of the end of a log the job page and `krater logs` show by default.
LOG_TAIL_BYTES = 64 * 1024
#: A download URL works this long: long enough to start a big download, short enough not to matter if it leaks.
DOWNLOAD_EXPIRES_SECONDS = 15 * 60
_LOG_NAME = re.compile(r"^attempt-(\d+)\.log$")


@dataclass(frozen=True)
class JobFile:
    area: str
    name: str
    size_bytes: int


@dataclass(frozen=True)
class LogTail:
    attempt: int
    text: str
    size_bytes: int
    truncated: bool  # the log is longer than what's in `text`


def can_read(actor: Actor, project: Project) -> bool:
    is_team = teams.on_team(project, actor.user.id)
    return is_team or actor.is_admin


def _require(actor: Actor, project: Project) -> None:
    if not can_read(actor, project):
        raise NotAllowed("Only the project's team and admins can read a job's output and files.")


def _prefix(job: ComputeJob, area: str) -> str:
    return f"projects/{job.project_id}/jobs/{job.id}/{area}/"


def list_files(store: ObjectStore, actor: Actor, *, job: ComputeJob) -> list[JobFile]:
    """Every file the job left, by area (outputs, checkpoints, logs) and name."""
    _require(actor, job.project)
    files = []
    for area in AREAS:
        prefix = _prefix(job, area)
        files += [JobFile(area, item.key[len(prefix) :], item.size_bytes) for item in store.list_prefix(prefix)]
    return sorted(files, key=lambda f: (AREAS.index(f.area), f.name))


def download_url(store: ObjectStore, actor: Actor, *, job: ComputeJob, area: str, name: str) -> str:
    """A short-lived URL for one of the job's files. Only for a file that exists, so a name can't reach elsewhere."""
    _require(actor, job.project)
    if area not in AREAS or not any(f.area == area and f.name == name for f in list_files(store, actor, job=job)):
        raise NotFound("No such file.")
    return store.presign_download(_prefix(job, area) + name, expires=DOWNLOAD_EXPIRES_SECONDS)


def list_downloads(store: ObjectStore, actor: Actor, *, job: ComputeJob) -> list[tuple[JobFile, str]]:
    """Every file the job left with a short-lived download URL, from one listing (`krater download`)."""
    return [
        (f, store.presign_download(_prefix(job, f.area) + f.name, expires=DOWNLOAD_EXPIRES_SECONDS))
        for f in list_files(store, actor, job=job)
    ]


def log_tail(
    store: ObjectStore, actor: Actor, *, job: ComputeJob, attempt: int | None = None, max_bytes: int = LOG_TAIL_BYTES
) -> LogTail | None:
    """The end of the job's output log: `attempt`'s, or the latest attempt that has one. `None` if there's none yet."""
    _require(actor, job.project)
    logs = {}
    for item in store.list_prefix(_prefix(job, "logs")):
        match = _LOG_NAME.match(item.key.rsplit("/", 1)[-1])
        if match:
            logs[int(match[1])] = item
    if not logs:
        return None
    chosen = attempt if attempt is not None else max(logs)
    item = logs.get(chosen)
    if item is None:
        return None
    raw = store.read_tail(item.key, max_bytes) or b""
    truncated = item.size_bytes > len(raw)
    text = raw.decode("utf-8", errors="replace")
    if truncated and "\n" in text:
        text = text.split("\n", 1)[1]  # start at a whole line
    return LogTail(attempt=chosen, text=text, size_bytes=item.size_bytes, truncated=truncated)
