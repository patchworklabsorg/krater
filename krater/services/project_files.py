"""Everything a project stores in Krater, in one place: its inputs, its boxes' workspaces, and each job's outputs,
checkpoints and logs, against its storage quota. For the project's Files page, `GET /api/v1/projects/{id}/files` and
`krater files`.

One listing of the project's prefix (`projects/<project>/`) answers all of it. Only the project's team and admins may
see it, as for a job's files (`compute_files`): stored files can hold anything a job wrote.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeJob, ComputeWorkspace, Project
from krater.services import compute_files, compute_settings, workspaces
from krater.services.actor import Actor
from krater.services.errors import NotAllowed
from krater.storage import ObjectStore

#: The areas of a job's own files, in the order the page lists them.
JOB_AREAS = ("outputs", "checkpoints", "logs")


@dataclass
class Tally:
    files: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size


@dataclass
class WorkspaceSummary:
    workspace: ComputeWorkspace
    stored: Tally
    holder: ComputeJob | None


@dataclass
class JobSummary:
    job: ComputeJob
    areas: dict[str, Tally] = field(default_factory=dict)

    @property
    def total(self) -> Tally:
        return Tally(sum(t.files for t in self.areas.values()), sum(t.bytes for t in self.areas.values()))


@dataclass
class ProjectFiles:
    quota_bytes: int
    total: Tally
    inputs: Tally
    workspaces: list[WorkspaceSummary]
    jobs: list[JobSummary]
    # Stored under the project but none of the above (a deleted workspace's leftovers, an unfinished upload's parts).
    other: Tally

    @property
    def percent_used(self) -> int:
        return min(100, round(100 * self.total.bytes / self.quota_bytes)) if self.quota_bytes else 0


def summary(session: Session, store: ObjectStore, actor: Actor, project: Project) -> ProjectFiles:
    """What the project stores, by kind, from one listing. Raises `NotAllowed` for anyone but its team and admins."""
    if not compute_files.can_read(actor, project):
        raise NotAllowed("Only the project's team and admins can see its stored files.")
    base = f"projects/{project.id}/"
    live = workspaces.list_workspaces(session, project)
    by_prefix = {ws.prefix: ws for ws in live}
    held = workspaces.holders(session, project)
    jobs = {
        job.id: job
        for job in session.scalars(
            sa.select(ComputeJob).where(ComputeJob.project_id == project.id).order_by(ComputeJob.created_at.desc())
        )
    }
    total, inputs, other = Tally(), Tally(), Tally()
    ws_tally: dict[uuid.UUID, Tally] = defaultdict(Tally)
    job_tally: dict[uuid.UUID, dict[str, Tally]] = defaultdict(lambda: defaultdict(Tally))
    for item in store.list_prefix(base):
        total.add(item.size_bytes)
        rest = item.key[len(base) :]
        if rest.startswith("inputs/"):
            inputs.add(item.size_bytes)
            continue
        parts = rest.split("/", 3)
        if len(parts) == 4 and parts[0] == "jobs":
            prefix = f"{base}jobs/{parts[1]}/{parts[2]}/"
            if parts[2] == "workspace" and prefix in by_prefix:
                ws_tally[by_prefix[prefix].id].add(item.size_bytes)
                continue
            try:
                job_id = uuid.UUID(parts[1])
            except ValueError:
                job_id = None
            if job_id in jobs and parts[2] in JOB_AREAS:
                job_tally[job_id][parts[2]].add(item.size_bytes)
                continue
        other.add(item.size_bytes)
    quota = compute_settings.effective_policy(session, project).storage_quota_gb * 1024**3
    return ProjectFiles(
        quota_bytes=quota,
        total=total,
        inputs=inputs,
        workspaces=[WorkspaceSummary(ws, ws_tally.get(ws.id, Tally()), held.get(ws.id)) for ws in live],
        jobs=[
            JobSummary(job, {area: job_tally[job.id][area] for area in JOB_AREAS if area in job_tally[job.id]})
            for job in jobs.values()
            if job.id in job_tally
        ],
        other=other,
    )
