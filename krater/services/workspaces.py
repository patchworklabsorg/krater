"""Box workspaces: a box's saved /workspace, kept apart from the box so a later box can continue it.

Every box queued has a workspace (`ComputeJob.workspace`): a new one, named after the box, or one an earlier box of the
same project saved. The runner saves /workspace there every 10 minutes and before Krater stops the box, and restores
it when the box starts on a machine (`runner/workspace.go`, through `krater.compute.runner_api`). A workspace outlives
its boxes: destroying a box ends its machine and disk, not its workspace, which the team can continue in a new box,
browse and download here, or delete. It goes with the rest of the project's files once the project is finished
(`compute_watch.delete_due_files`).

**One box at a time.** A box that isn't finished (queued, starting, running, paused or stopped) holds its workspace.
A second box saving to the same files would overwrite the first one's saves and, since a save removes from storage
what its machine deleted, remove the other box's new files. So continuing a workspace locks its row and refuses one
that a box still holds, unless the member asks to end that box first, which only a stopped box allows (its stop
already saved /workspace, or the page warned that it didn't).

**Who.** The project's whole team, and admins, can see, browse, download, continue and delete any of its workspaces,
as they can stop, resume and end any of its boxes. Continuing one gives the new box's owner SSH access to what is in
it, so the pages say so: keep credentials in the project's secrets, not in /workspace.

Framework-free: the job form, the Files page, the API and the CLI call it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeEvent, ComputeJob, ComputeJobKind, ComputeJobState, ComputeWorkspace, Project
from krater.models.compute_job import FINISHED_JOB_STATES
from krater.services import audit, teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.storage import ObjectStore

AUDIT_WORKSPACE_DELETED = "compute_workspace_deleted"
#: The runner's manifest of a saved workspace (`runner/workspace.go` `manifestName`).
MANIFEST = ".krater-workspace.json"
#: Krater's own files in a workspace's storage, never listed as the member's: the manifest and hashed names.
_OWN_PREFIXES = (".krater-workspace.json", ".krater-files/")
#: The most of a manifest Krater reads. 12,000 files make about 2 MB.
MANIFEST_MAX_BYTES = 64 * 1024 * 1024
#: A download URL works this long, as for a job's files.
DOWNLOAD_EXPIRES_SECONDS = 15 * 60
NAME_MAX = 200


def job_prefix(job: ComputeJob) -> str:
    """The per-job path of a box's own files: where every workspace's files live (the box that started it)."""
    return f"projects/{job.project_id}/jobs/{job.id}/workspace/"


def prefix_for(job: ComputeJob) -> str:
    """Where `job`'s runner saves and restores /workspace: its workspace's files, or (a box queued before workspaces,
    which has none) its own per-job path."""
    return job.workspace.prefix if job.workspace is not None else job_prefix(job)


def can_see(actor: Actor, project: Project) -> bool:
    return actor.is_admin or teams.on_team(project, actor.user.id)


def _require_team(actor: Actor, project: Project) -> None:
    if not can_see(actor, project):
        raise NotAllowed("Only the project's team and admins can use its workspaces.")


def check_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ValidationFailed({"workspace_name": "Give the workspace a name."})
    if len(name) > NAME_MAX:
        raise ValidationFailed({"workspace_name": f"Keep the workspace's name under {NAME_MAX} characters."})
    return name


def holder(session: Session, workspace: ComputeWorkspace) -> ComputeJob | None:
    """The box that holds `workspace` now: one that isn't finished. At most one, by the lock in `attach`."""
    return session.scalar(
        sa.select(ComputeJob)
        .where(ComputeJob.workspace_id == workspace.id, ComputeJob.state.not_in(FINISHED_JOB_STATES))
        .order_by(ComputeJob.created_at.desc())
        .limit(1)
    )


def holders(session: Session, project: Project) -> dict[uuid.UUID, ComputeJob]:
    """Each of the project's held workspaces, by id, with the box holding it."""
    rows = session.scalars(
        sa.select(ComputeJob).where(
            ComputeJob.project_id == project.id,
            ComputeJob.workspace_id.is_not(None),
            ComputeJob.state.not_in(FINISHED_JOB_STATES),
        )
    )
    return {job.workspace_id: job for job in rows}


def list_workspaces(session: Session, project: Project) -> list[ComputeWorkspace]:
    """The project's workspaces, newest save first (never-saved ones last, newest first)."""
    rows = session.scalars(
        sa.select(ComputeWorkspace).where(
            ComputeWorkspace.project_id == project.id, ComputeWorkspace.deleted_at.is_(None)
        )
    )
    return sorted(rows, key=lambda w: (w.last_saved_at is not None, w.last_saved_at or w.created_at), reverse=True)


def get(session: Session, actor: Actor, project: Project, workspace_id: uuid.UUID) -> ComputeWorkspace:
    _require_team(actor, project)
    workspace = session.get(ComputeWorkspace, workspace_id)
    if workspace is None or workspace.project_id != project.id or workspace.deleted_at is not None:
        raise NotFound("No such workspace.")
    return workspace


def find(session: Session, project: Project, ref: str) -> ComputeWorkspace | None:
    """A workspace of the project by id, id prefix (8+ characters) or exact name, for the CLI's `--workspace`."""
    live = list_workspaces(session, project)
    ref = ref.strip()
    for workspace in live:
        if str(workspace.id) == ref:
            return workspace
    by_prefix = [w for w in live if len(ref) >= 8 and str(w.id).startswith(ref)]
    if len(by_prefix) == 1:
        return by_prefix[0]
    by_name = [w for w in live if w.name == ref]
    if len(by_name) > 1:
        raise ValidationFailed({"workspace": f"More than one workspace is named {ref!r}; give its id."})
    return by_name[0] if by_name else None


def start_new(session: Session, job: ComputeJob, *, name: str | None = None) -> ComputeWorkspace:
    """A new workspace for box `job`, stored at its per-job path. Flushes."""
    workspace = ComputeWorkspace(
        project_id=job.project_id,
        name=check_name(name) if name else job.name,
        created_by_id=job.submitter_id,
        prefix=job_prefix(job),
        origin_job_id=job.id,
    )
    session.add(workspace)
    session.flush()
    job.workspace_id = workspace.id
    job.workspace = workspace
    session.flush()
    return workspace


def attach(
    session: Session,
    actor: Actor,
    *,
    job: ComputeJob,
    workspace_id: uuid.UUID,
    end_holder: bool,
    now: datetime,
) -> ComputeWorkspace:
    """Box `job` continues the workspace `workspace_id`. Locks the workspace's row, so two boxes can't take it at
    once, and refuses one a box still holds, unless `end_holder` and that box is stopped: it is then ended (its
    machine destroyed on the scheduler's next pass) and `job` continues from its last save. Flushes."""
    project = job.project
    _require_team(actor, project)
    workspace = session.scalar(sa.select(ComputeWorkspace).where(ComputeWorkspace.id == workspace_id).with_for_update())
    if workspace is None or workspace.project_id != project.id or workspace.deleted_at is not None:
        raise ValidationFailed({"workspace": "That workspace isn't one of this project's."})
    held_by = holder(session, workspace)
    if held_by is not None and held_by.id != job.id:
        if held_by.state is not ComputeJobState.STOPPED:
            raise ValidationFailed(
                {
                    "workspace": f"The workspace {workspace.name!r} is in use by the box {held_by.name!r} "
                    f"({held_by.state.value}). Stop or end that box first (both save /workspace), or start a new "
                    "workspace."
                }
            )
        if not end_holder:
            raise ValidationFailed(
                {
                    "workspace": f"The workspace {workspace.name!r} is in use by the stopped box {held_by.name!r}. "
                    "End it to continue here (tick the box, or `krater submit --end-holder`), or start a new "
                    "workspace."
                }
            )
        _end_for(session, actor, held_by, successor=job, now=now)
    job.workspace_id = workspace.id
    job.workspace = workspace
    session.add(
        ComputeEvent(
            job_id=job.id,
            kind="workspace_continued",
            message=f"Continues the workspace {workspace.name!r}"
            + (f", last saved {workspace.last_saved_at:%Y-%m-%d %H:%M UTC}." if workspace.last_saved_at else "."),
            payload={"workspace_id": str(workspace.id), "by": str(actor.user.id)},
            at=now,
        )
    )
    session.flush()
    return workspace


def _end_for(session: Session, actor: Actor, box: ComputeJob, *, successor: ComputeJob, now: datetime) -> None:
    """End a stopped box so `successor` can continue its workspace (its machine goes on the scheduler's next pass)."""
    box.state = ComputeJobState.CANCELLED
    box.state_reason = f"Ended so the box {successor.name!r} can continue its workspace."
    box.ended_at = now
    box.hold_cents = 0
    session.add(
        ComputeEvent(
            job_id=box.id,
            kind="cancelled",
            message=box.state_reason,
            payload={"by": str(actor.user.id), "successor": str(successor.id)},
            at=now,
        )
    )


def last_stop_saved(box: ComputeJob) -> bool:
    """Whether a stopped box's last stop saved /workspace (Krater asked, and the runner confirmed). An emergency stop,
    or a box Krater lost touch with, stops without one: its disk then holds newer work than its saved workspace."""
    live = [row for row in box.instances if row.is_live]
    row = live[-1] if live else None
    if row is None or row.stopped_at is None:
        return True
    return row.last_synced_at is not None and row.last_synced_at >= row.stopped_at - _STOP_SAVE_SLACK


#: How long before a box stopped its confirmed save may be and still count as its stop-save (past the sync deadline).
_STOP_SAVE_SLACK = timedelta(minutes=15)


def record_save(job: ComputeJob, saved_at: datetime, files: int | None, size: int | None) -> None:
    """A save of `job`'s /workspace stored everything, as of `saved_at`."""
    workspace = job.workspace
    if workspace is None:
        return
    if workspace.last_saved_at is None or saved_at >= workspace.last_saved_at:
        workspace.last_saved_at = saved_at
        if files is not None:
            workspace.files = files
        if size is not None:
            workspace.bytes = size


def delete(session: Session, store: ObjectStore, actor: Actor, *, workspace: ComputeWorkspace, now: datetime) -> int:
    """Delete `workspace` and its stored files, by its team or an admin, unless a box holds it. Returns how many
    files went. Writes an `AuditEvent`."""
    project = session.get(Project, workspace.project_id)
    _require_team(actor, project)
    if workspace.deleted_at is not None:
        raise NotFound("No such workspace.")
    held_by = holder(session, workspace)
    if held_by is not None:
        raise InvalidState(
            f"The box {held_by.name!r} ({held_by.state.value}) uses this workspace. End it first, then delete it."
        )
    objects = store.list_prefix(workspace.prefix)
    for item in objects:
        store.delete(item.key)
    workspace.deleted_at = now
    workspace.deleted_by_id = actor.user.id
    audit.record(
        session,
        actor,
        AUDIT_WORKSPACE_DELETED,
        project=project,
        payload={
            "workspace_id": str(workspace.id),
            "name": workspace.name,
            "objects": len(objects),
            "bytes": sum(item.size_bytes for item in objects),
        },
    )
    session.flush()
    return len(objects)


# -- Its files -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceEntry:
    """One path in a saved workspace, as the member named it: a file, a folder or a symlink."""

    path: str
    kind: str  # "file", "dir" or "link"
    size_bytes: int = 0
    mode: int = 0
    mtime_ns: int = 0
    target: str = ""  # a symlink's
    stored: str = ""  # the file's name in storage (its path, or a hashed name)


@dataclass(frozen=True)
class WorkspaceListing:
    entries: list[WorkspaceEntry]
    # Whether the runner's manifest was there (a workspace saved before 2026-11-12 has plain files only).
    has_manifest: bool
    stored_bytes: int


def _read_manifest(store: ObjectStore, prefix: str) -> list[dict] | None:
    raw = store.read_prefix(prefix + MANIFEST, MANIFEST_MAX_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else None


def listing(store: ObjectStore, workspace: ComputeWorkspace) -> WorkspaceListing:
    """Everything in the saved workspace, by the member's own paths. With a manifest, only what it names and storage
    holds (a file whose upload failed is named with its last stored copy); without one, the stored files."""
    objects = store.list_prefix(workspace.prefix)
    stored = {item.key[len(workspace.prefix) :]: item.size_bytes for item in objects}
    total = sum(stored.values())
    manifest = _read_manifest(store, workspace.prefix)
    if manifest is None:
        entries = [
            WorkspaceEntry(path=name, kind="file", size_bytes=size, stored=name)
            for name, size in sorted(stored.items())
            if not name.startswith(_OWN_PREFIXES)
        ]
        return WorkspaceListing(entries=entries, has_manifest=False, stored_bytes=total)
    entries = []
    for raw in manifest:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            continue
        kind = raw.get("type")
        if kind == "file":
            name = raw.get("stored") or raw["path"]
            if name not in stored:
                continue
            entries.append(
                WorkspaceEntry(
                    path=raw["path"],
                    kind="file",
                    size_bytes=stored[name],
                    mode=int(raw.get("mode") or 0),
                    mtime_ns=int(raw.get("mtime") or 0),
                    stored=name,
                )
            )
        elif kind in ("dir", "link"):
            entries.append(
                WorkspaceEntry(
                    path=raw["path"],
                    kind=kind,
                    mode=int(raw.get("mode") or 0),
                    target=str(raw.get("target") or ""),
                )
            )
    entries.sort(key=lambda e: e.path)
    return WorkspaceListing(entries=entries, has_manifest=True, stored_bytes=total)


def download_url(store: ObjectStore, workspace: ComputeWorkspace, path: str) -> str:
    """A short-lived URL for the file at the member's `path` in the saved workspace. Only for a listed file, so a
    path can't reach elsewhere."""
    for entry in listing(store, workspace).entries:
        if entry.kind == "file" and entry.path == path:
            return store.presign_download(workspace.prefix + entry.stored, expires=DOWNLOAD_EXPIRES_SECONDS)
    raise NotFound("No such file in this workspace.")


def downloads(store: ObjectStore, workspace: ComputeWorkspace) -> tuple[WorkspaceListing, dict[str, str]]:
    """The listing, and a short-lived URL for each file by path (`krater download --workspace`)."""
    shown = listing(store, workspace)
    urls = {
        e.path: store.presign_download(workspace.prefix + e.stored, expires=DOWNLOAD_EXPIRES_SECONDS)
        for e in shown.entries
        if e.kind == "file"
    }
    return shown, urls


def require_box(job: ComputeJob) -> None:
    if job.kind is not ComputeJobKind.BOX:
        raise ValidationFailed({"workspace": "Only a box has a workspace."})
