"""What the runner on a rented machine may ask Krater, and nothing more.

The runner authenticates with its instance's token (`KRATER_JOB_TOKEN`, generated when the instance is rented;
Krater keeps only its sha256). A token works only while its instance is live and not being destroyed, so a moved
job's old machine is locked out. With it the runner can:

- `start`: say it started, and learn its command, whether this is a restart, its timings and its project's secrets
  (`krater.services.project_secrets`: they reach the machine only this way, never in Vast's create request);
- `heartbeat`: report GPU use and open sessions, and learn whether to stop;
- `exit`: report the command's exit code (the scheduler destroys the machine on its next pass);
- `checkpoints`: list, download and upload its own job's checkpoints and outputs, through presigned URLs.

Framework-free; `krater.web.routers.runner` maps it onto HTTP.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater import providers
from krater.config import Settings
from krater.models import (
    ComputeEvent,
    ComputeInstance,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    Project,
    StorageReservation,
)
from krater.models.compute_job import ACTIVE_JOB_STATES
from krater.services import box_access, compute_settings, project_secrets
from krater.services.errors import NotAllowed, ValidationFailed
from krater.storage import ObjectStore

#: The runner's timings, sent with `start` so they can change without a new runner build.
HEARTBEAT_SECONDS = 60
CHECKPOINT_SYNC_SECONDS = 600
#: How often the job's output log goes up, so its team can watch it on the job page.
LOG_SYNC_SECONDS = 60
#: Uploads at most this big go up in one PUT; bigger ones in parts of this size. Under Cloudflare's 100 MB cap on a
#: proxied request body.
PART_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BYTES = 500 * 1024**3
#: Presigned URLs live this long: a 64 MB part on a slow link still makes it.
URL_EXPIRES_SECONDS = 3600

AREAS = ("checkpoints", "outputs", "workspace", "logs", "inputs")
#: The project's inputs (`krater.services.compute_inputs`): a machine may list and download the ones its job asked
#: for, and never change them.
INPUTS = "inputs"
WORKSPACE = "workspace"
# How many stored files one delete call may name, and one upload call may begin.
MAX_DELETE_NAMES = 1000
MAX_BATCH_UPLOADS = 500
# Wide enough for the names ML tools write (Lightning's `epoch=3-step=100.ckpt`), with no control characters or
# backslashes, which would trip the CLI writing them to a member's disk.
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-=+,@~][A-Za-z0-9._\-=+,@~ /]{0,511}$")


def new_token() -> tuple[str, str]:
    """A fresh runner token and the hash Krater stores."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(session: Session, token: str) -> ComputeInstance:
    """The live instance `token` belongs to. Raises `NotAllowed` for anything else (unknown, destroyed, being
    destroyed, or its job finished)."""
    if not token or len(token) > 200:
        raise NotAllowed("No runner token.")
    instance = session.scalar(sa.select(ComputeInstance).where(ComputeInstance.runner_token_hash == hash_token(token)))
    if instance is None or not instance.is_live or instance.destroy_requested_at is not None:
        raise NotAllowed("This machine is no longer Krater's to run.")
    return instance


def _event(session: Session, instance: ComputeInstance, kind: str, message: str, *, now: datetime, **payload):
    session.add(
        ComputeEvent(
            job_id=instance.job_id, instance_id=instance.id, kind=kind, message=message, payload=payload, at=now
        )
    )


@dataclass(frozen=True)
class StartInfo:
    job_id: str
    attempt: int
    resumed: bool
    command: str | None
    kind: str
    # A box's authorized_keys: its submitter's SSH keys and every admin's (`box_access`), as `type base64` lines.
    ssh_keys: tuple[str, ...] = ()
    heartbeat_seconds: int = HEARTBEAT_SECONDS
    checkpoint_sync_seconds: int = CHECKPOINT_SYNC_SECONDS
    log_sync_seconds: int = LOG_SYNC_SECONDS
    # With no answer from Krater for this long, the runner stops its own machine (the policy's
    # `runner_self_stop_minutes`), through Vast's API with the instance's own key, which Vast puts in the container.
    # -1 turns the switch off, for a provider whose machines can't stop themselves (`Capabilities.self_stop`).
    self_stop_after_seconds: int = 0
    # How the runner stops its machine: `{"method": "vast_api", "url", "instance_id"}`, `{"method": "shutdown"}`, or
    # None when the provider has no self-stop. Built from the provider's `self_stop_method`, never from its name.
    self_stop: dict[str, str] | None = None
    # Whether the job asked for any of its project's inputs: the runner copies them in before the command.
    inputs: bool = False
    # Legacy wire names, still sent for runners already on machines, which know only Vast's integer id for the
    # machine and its API's address (`runner/api.go` ignores them when `self_stop` is present).
    vast_instance_id: int | None = None
    vast_api_url: str = ""
    # The project's secrets, name -> value. Never logged or shown (kept out of the repr).
    secrets: dict[str, str] = field(default_factory=dict, repr=False)


def _vast_wire_id(instance: ComputeInstance) -> int | None:
    pid = instance.provider_instance_id
    if instance.provider != "vast" or pid is None or not (pid.isascii() and pid.isdigit()):
        return None
    return int(pid)


def _self_stop_descriptor(method: str | None, instance: ComputeInstance, settings: Settings) -> dict[str, str] | None:
    if method == "vast_api":
        # The id is a string for every provider (AWS ids aren't integers); the runner parses what its method needs.
        return {"method": method, "url": settings.vast_api_url, "instance_id": instance.provider_instance_id or ""}
    if method == "shutdown":
        return {"method": method}
    return None


def start(session: Session, instance: ComputeInstance, *, now: datetime, settings: Settings) -> StartInfo:
    """The runner started (again). A restart on the same machine (Vast restarts the container after a pause) or a
    later attempt on another one is a resume: the member's code should restore its last checkpoint."""
    job = instance.job
    if job.state not in ACTIVE_JOB_STATES:
        raise NotAllowed("This job has ended.")
    instance.runner_starts += 1
    instance.runner_started_at = now
    instance.last_heartbeat_at = now
    resumed = instance.attempt > 1 or instance.runner_starts > 1
    ran_before = instance.runner_starts > 1 or any(
        other.runner_starts > 0 for other in job.instances if other.attempt < instance.attempt
    )
    if not resumed:
        said = "The runner started."
    elif ran_before:
        said = "The runner started. Resuming."
    else:  # KRATER_RESUMED is still 1, as documented for any later attempt, but nothing ran before to resume from
        said = f"The runner started (attempt {instance.attempt}; no earlier attempt got this far)."
    _event(
        session,
        instance,
        "runner_started",
        said,
        now=now,
        runner_starts=instance.runner_starts,
    )
    session.flush()
    keys = tuple(box_access.authorized_keys(session, job, now=now)) if job.kind is ComputeJobKind.BOX else ()
    secrets_env = project_secrets.environment(session, job.project_id, settings)
    provider = providers.active(settings)
    if provider is None or provider.capabilities.self_stop:
        self_stop_seconds = compute_settings.effective_policy(session, job.project).runner_self_stop_minutes * 60
    else:
        self_stop_seconds = -1
    self_stop = _self_stop_descriptor(provider.self_stop_method, instance, settings) if provider else None
    return StartInfo(
        job_id=str(job.id),
        attempt=instance.attempt,
        resumed=resumed,
        command=job.command,
        kind=job.kind.value,
        ssh_keys=keys,
        self_stop_after_seconds=self_stop_seconds,
        self_stop=self_stop,
        vast_instance_id=_vast_wire_id(instance),
        vast_api_url=settings.vast_api_url,
        secrets=secrets_env,
        inputs=bool(job.input_names),
    )


# `key_updates`: the runner applies `ssh_keys` from a heartbeat reply (`krater.services.box_access`).
_HEARTBEAT_FIELDS = {
    "gpu_util": (int, float),
    "cpu_util": (int, float),
    "sessions": int,
    "phase": str,
    "key_updates": bool,
    # Bytes the container received and sent since its job's first start on this machine (runner/netcount.go).
    "net_in_bytes": int,
    "net_out_bytes": int,
}

#: The link speed assumed when the host's offer gave none, Mbps.
DEFAULT_LINK_MBPS = 10_000
#: Headroom over the host's measured speed: Vast's figure is a past measurement, not a hard limit.
LINK_SLACK = 1.5
#: The interval a report may cover at least, seconds: a first report covers the start, restores included.
MIN_REPORT_SECONDS = 120


def _bounded(reported: Any, previous: int, mbps: int | None, seconds: float) -> int:
    """The new total: what the runner reported, never less than before, and never more than the link could have
    carried since the last report. The runner runs the member's code as root and holds the token, so a report is a
    claim: it may only raise the estimate, and only by what is physically possible (it can't trip the project's
    budget stop or the organization's spending caps with an invented terabyte)."""
    if not isinstance(reported, int) or isinstance(reported, bool) or reported <= previous:
        return previous
    allowance = int((mbps or DEFAULT_LINK_MBPS) * 1_000_000 / 8 * max(seconds, MIN_REPORT_SECONDS) * LINK_SLACK)
    return min(reported, previous + allowance)


def _record_net_use(instance: ComputeInstance, kept: dict[str, Any], *, now: datetime) -> None:
    if "net_in_bytes" not in kept and "net_out_bytes" not in kept:
        return  # an older runner, or one that can't tell it sees only its own network
    since = instance.net_reported_at or instance.created_at or now
    seconds = (now - since).total_seconds()
    instance.net_in_bytes = _bounded(kept.get("net_in_bytes"), instance.net_in_bytes, instance.link_down_mbps, seconds)
    instance.net_out_bytes = _bounded(kept.get("net_out_bytes"), instance.net_out_bytes, instance.link_up_mbps, seconds)
    instance.net_reported_at = now


@dataclass(frozen=True)
class HeartbeatReply:
    stop: bool  # the job has ended: stop the command
    sync: bool  # save /workspace (a box) or the checkpoints (a batch job) and confirm: Krater is about to stop it
    # A box's whole authorized_keys: the owner's keys and every admin's (`box_access`). None for a batch job, and the
    # runner leaves the file alone when the reply has none.
    ssh_keys: tuple[str, ...] | None = None


def heartbeat(session: Session, instance: ComputeInstance, payload: dict[str, Any], *, now: datetime) -> HeartbeatReply:
    """Record a heartbeat and say what Krater wants next."""
    kept = {k: v for k, v in payload.items() if k in _HEARTBEAT_FIELDS and isinstance(v, _HEARTBEAT_FIELDS[k])}
    if isinstance(kept.get("phase"), str):
        kept["phase"] = kept["phase"][:32]
    instance.last_heartbeat_at = now
    instance.last_heartbeat = kept
    _record_net_use(instance, kept, now=now)
    session.flush()
    wants_sync = instance.stop_requested_at is not None and (
        instance.last_synced_at is None or instance.last_synced_at < instance.stop_requested_at
    )
    job = instance.job
    keys = tuple(box_access.authorized_keys(session, job, now=now)) if job.kind is ComputeJobKind.BOX else None
    return HeartbeatReply(stop=job.state not in ACTIVE_JOB_STATES, sync=wants_sync, ssh_keys=keys)


def synced(session: Session, instance: ComputeInstance, *, now: datetime) -> None:
    """The runner saved /workspace (a box) or its checkpoints (a batch job, when a regular stop of all compute asked).
    Once Krater asked for it, the scheduler stops the machine on its next pass."""
    instance.last_synced_at = now
    saved = "The box saved /workspace." if instance.job.kind is ComputeJobKind.BOX else "The job saved its checkpoints."
    _event(session, instance, "synced", saved, now=now)
    session.flush()


def report_exit(session: Session, instance: ComputeInstance, exit_code: int, *, now: datetime) -> ComputeJob:
    """The member's command exited. Idempotent: only the first report counts. The scheduler destroys the machine."""
    job = instance.job
    if job.state in ACTIVE_JOB_STATES:
        job.exit_code = exit_code
        job.state = ComputeJobState.SUCCEEDED if exit_code == 0 else ComputeJobState.FAILED
        job.state_reason = "Finished." if exit_code == 0 else f"The command exited with code {exit_code}."
        job.ended_at = now
        job.hold_cents = 0
        _event(session, instance, "exited", job.state_reason, now=now, exit_code=exit_code)
        session.flush()
    return job


# -- Checkpoints and outputs -------------------------------------------------------------------------------


def inputs_prefix(project_id: uuid.UUID) -> str:
    return f"projects/{project_id}/inputs/"


def _prefix(instance: ComputeInstance, area: str) -> str:
    if area not in AREAS:
        raise ValidationFailed({"area": f"area must be one of {', '.join(AREAS)}"})
    job = instance.job
    if area == INPUTS:
        return inputs_prefix(job.project_id)
    return f"projects/{job.project_id}/jobs/{job.id}/{area}/"


def check_name(name: str) -> str:
    """`name` if it's a usable relative path for a stored file; raises `ValidationFailed` otherwise."""
    segments = name.split("/")
    if not _NAME_RE.match(name) or any(s in ("", ".", "..") for s in segments) or name != name.strip():
        raise ValidationFailed(
            {"name": "Use a relative path of letters, digits, spaces and . _ - = + , @ ~, with '/' between folders."}
        )
    return name


def _key(instance: ComputeInstance, area: str, name: str) -> str:
    return _prefix(instance, area) + check_name(name)


def selected(names: list[str], wanted: list[str]) -> list[str]:
    """The stored input `names` a job's `wanted` list picks: exact names, and everything under a name ending in /."""
    folders = tuple(w for w in wanted if w.endswith("/"))
    exact = set(wanted)
    return [n for n in names if n in exact or (folders and n.startswith(folders))]


def list_files(store: ObjectStore, instance: ComputeInstance, area: str) -> list[dict[str, Any]]:
    prefix = _prefix(instance, area)
    items = store.list_prefix(prefix)
    if area == INPUTS:
        # Only what the job asked for; a job that asked for none gets none.
        stored = [item.key[len(prefix) :] for item in items]
        wanted = list(instance.job.input_names or [])
        missing = [w for w in wanted if not selected(stored, [w])]
        if missing:
            # Deleted since the job was queued: the runner fails the job rather than run it without them.
            raise ValidationFailed({"inputs": f"The job's inputs {', '.join(missing)} are no longer stored."})
        chosen = set(selected(stored, wanted))
        items = [item for item in items if item.key[len(prefix) :] in chosen]
    return [
        {
            "name": item.key[len(prefix) :],
            "size": item.size_bytes,
            "url": store.presign_download(item.key, expires=URL_EXPIRES_SECONDS),
        }
        for item in items
    ]


def project_usage_bytes(store: ObjectStore, instance: ComputeInstance) -> dict[str, int]:
    """Bytes stored per key under the instance's project."""
    return _usage(store, instance.job.project_id)


def _usage(store: ObjectStore, project_id: uuid.UUID) -> dict[str, int]:
    prefix = f"projects/{project_id}/"
    return {item.key: item.size_bytes for item in store.list_prefix(prefix)}


def _no_inputs(area: str) -> None:
    if area == INPUTS:
        raise NotAllowed("A machine can read its project's inputs but not change them.")


def begin_upload(
    session: Session,
    store: ObjectStore,
    instance: ComputeInstance,
    area: str,
    name: str,
    size: int,
    *,
    quota_bytes: int,
    now: datetime,
) -> dict[str, Any]:
    """One presigned PUT for a small file; for a big one, a multipart upload with a presigned URL per part. Each URL
    is signed for its exact size, and the upload must fit the project's storage quota (replacing a file counts only
    the difference).

    Storage lists a file only once its upload finishes, so the size is reserved (`StorageReservation`) until the URLs
    expire, and the quota counts each file at the larger of what's stored and what's reserved for it. The project's
    row lock makes two uploads check the quota one after the other. The caller commits."""
    _no_inputs(area)
    return reserve_upload(
        session,
        store,
        instance.job.project_id,
        _key(instance, area, name),
        name,
        size,
        quota_bytes=quota_bytes,
        now=now,
    )


def reserve_upload(
    session: Session,
    store: ObjectStore,
    project_id: uuid.UUID,
    key: str,
    name: str,
    size: int,
    *,
    quota_bytes: int,
    now: datetime,
) -> dict[str, Any]:
    """`begin_upload` for any key under the project (a machine's files, or a member's inputs)."""
    usage = _QuotaUsage.take(session, store, project_id, now=now)
    upload = usage.reserve(key, name, size, quota_bytes=quota_bytes)
    session.flush()
    return upload


class _QuotaUsage:
    """What a project stores, counting each key at the larger of its stored and reserved size, under the project's
    row lock. `reserve` checks one more upload against the quota and counts it, so a batch checks file N with files
    1 to N-1 already counted."""

    def __init__(
        self,
        session: Session,
        store: ObjectStore,
        project_id: uuid.UUID,
        usage: dict[str, int],
        reservations: dict[str, StorageReservation],
        now: datetime,
    ):
        self.session, self.store, self.project_id, self.now = session, store, project_id, now
        self.usage, self.reservations = usage, reservations
        self.total = sum(usage.values())

    @classmethod
    def take(cls, session: Session, store: ObjectStore, project_id: uuid.UUID, *, now: datetime) -> _QuotaUsage:
        session.execute(sa.select(Project.id).where(Project.id == project_id).with_for_update())
        session.execute(
            sa.delete(StorageReservation).where(
                StorageReservation.project_id == project_id, StorageReservation.expires_at <= now
            )
        )
        usage = _usage(store, project_id)
        # Loaded once, so a batch of uploads doesn't look each one up (a key holds its project's id: no other
        # project's reservation can have it).
        reservations = {
            r.key: r
            for r in session.scalars(sa.select(StorageReservation).where(StorageReservation.project_id == project_id))
        }
        for reserved_key, reservation in reservations.items():
            usage[reserved_key] = max(usage.get(reserved_key, 0), reservation.size_bytes)
        return cls(session, store, project_id, usage, reservations, now)

    def reserve(self, key: str, name: str, size: int, *, quota_bytes: int) -> dict[str, Any]:
        if not 0 <= size <= MAX_UPLOAD_BYTES:
            raise ValidationFailed({"size": f"Files can be up to {MAX_UPLOAD_BYTES // 1024**3} GB."})
        current = self.usage.get(key, 0)
        used = self.total - current
        if used + size > quota_bytes:
            raise ValidationFailed(
                {"size": f"The project's storage quota of {quota_bytes // 1024**3} GB is full ({used} bytes used)."}
            )
        reservation = self.reservations.get(key)
        if reservation is None:
            reservation = StorageReservation(key=key, project_id=self.project_id)
            self.session.add(reservation)
            self.reservations[key] = reservation
        reservation.size_bytes = size
        reservation.expires_at = self.now + timedelta(seconds=URL_EXPIRES_SECONDS)
        counted = max(current, size)
        self.usage[key] = counted
        self.total += counted - current
        return _upload_urls(self.store, key, name, size)


def _upload_urls(store: ObjectStore, key: str, name: str, size: int) -> dict[str, Any]:
    if size <= PART_BYTES:
        return {"name": name, "url": store.presign_put(key, content_length=size, expires=URL_EXPIRES_SECONDS)}
    upload_id = store.create_multipart_upload(key)
    parts = math.ceil(size / PART_BYTES)
    return {
        "name": name,
        "upload_id": upload_id,
        "part_bytes": PART_BYTES,
        "parts": [
            store.presign_upload_part(
                key,
                upload_id=upload_id,
                part_number=n,
                content_length=min(PART_BYTES, size - (n - 1) * PART_BYTES),
                expires=URL_EXPIRES_SECONDS,
            )
            for n in range(1, parts + 1)
        ],
    }


def begin_uploads(
    session: Session,
    store: ObjectStore,
    instance: ComputeInstance,
    area: str,
    files: Any,
    *,
    quota_bytes: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """`begin_upload` for up to MAX_BATCH_UPLOADS files in one call, with one quota count for all of them. A box's
    workspace can be thousands of small files, and one call per file (each listing the whole project) was too slow
    to save a Python virtual environment before its box stopped (staging, 2026-11-12: 2,357 files, over 10 minutes).

    A file that can't be uploaded (a bad name, a repeated name, the quota) gets `{"name", "error"}` in its place,
    and the others still get URLs. The caller commits."""
    _no_inputs(area)
    _prefix(instance, area)
    if not isinstance(files, list) or not 0 < len(files) <= MAX_BATCH_UPLOADS:
        raise ValidationFailed({"files": f"Send a list of 1 to {MAX_BATCH_UPLOADS} files."})
    wanted: list[tuple[str, int]] = []
    for item in files:
        name = item.get("name") if isinstance(item, dict) else None
        size = item.get("size") if isinstance(item, dict) else None
        if not isinstance(name, str) or not isinstance(size, int) or isinstance(size, bool):
            raise ValidationFailed({"files": "Each file needs a name (a string) and a size (an integer)."})
        wanted.append((name, size))
    usage = _QuotaUsage.take(session, store, instance.job.project_id, now=now)
    uploads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, size in wanted:
        try:
            if name in seen:
                raise ValidationFailed({"name": "The same name is twice in this batch."})
            seen.add(name)
            uploads.append(usage.reserve(_key(instance, area, name), name, size, quota_bytes=quota_bytes))
        except ValidationFailed as exc:
            uploads.append({"name": name, "error": "; ".join(exc.errors.values())})
    session.flush()
    return uploads


def complete_upload(
    store: ObjectStore, instance: ComputeInstance, area: str, name: str, upload_id: str, etags: list[str]
) -> None:
    _no_inputs(area)
    finish_upload(store, _key(instance, area, name), upload_id, etags)


def delete_files(store: ObjectStore, instance: ComputeInstance, area: str, names: Any) -> int:
    """Removes stored files of a box's workspace: the runner deletes what the member deleted on the machine, so it
    doesn't come back on the next restore (docs/compute/runner.md "Boxes"). Checkpoints, outputs, logs and inputs are
    never deleted by a machine."""
    if area != WORKSPACE or instance.job.kind is not ComputeJobKind.BOX:
        raise NotAllowed("A machine can delete files only from its box's /workspace.")
    if not isinstance(names, list) or len(names) > MAX_DELETE_NAMES or not all(isinstance(name, str) for name in names):
        raise ValidationFailed({"names": f"Send a list of up to {MAX_DELETE_NAMES} stored names."})
    keys = [_key(instance, area, name) for name in names]
    for key in keys:
        store.delete(key)
    return len(keys)


def finish_upload(store: ObjectStore, key: str, upload_id: str, etags: list[str]) -> None:
    if not upload_id or not etags or not all(isinstance(e, str) and e for e in etags):
        raise ValidationFailed({"etags": "Send the ETag of every part, in order."})
    store.complete_multipart_upload(key, upload_id=upload_id, parts=list(enumerate(etags, start=1)))
