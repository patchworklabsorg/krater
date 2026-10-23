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
from krater.services import compute_settings, project_secrets, ssh_keys
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

AREAS = ("checkpoints", "outputs", "workspace", "logs")
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
    # A box's authorized_keys: its submitter's SSH keys, as `type base64` lines.
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
    _event(
        session,
        instance,
        "runner_started",
        "The runner started." + (" Resuming." if resumed else ""),
        now=now,
        runner_starts=instance.runner_starts,
    )
    session.flush()
    keys = tuple(ssh_keys.authorized_keys_for(session, job.submitter_id)) if job.kind is ComputeJobKind.BOX else ()
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
    )


_HEARTBEAT_FIELDS = {"gpu_util": (int, float), "cpu_util": (int, float), "sessions": int, "phase": str}


@dataclass(frozen=True)
class HeartbeatReply:
    stop: bool  # the job has ended: stop the command
    sync: bool  # save /workspace and confirm: Krater is about to stop the box


def heartbeat(session: Session, instance: ComputeInstance, payload: dict[str, Any], *, now: datetime) -> HeartbeatReply:
    """Record a heartbeat and say what Krater wants next."""
    kept = {k: v for k, v in payload.items() if k in _HEARTBEAT_FIELDS and isinstance(v, _HEARTBEAT_FIELDS[k])}
    if isinstance(kept.get("phase"), str):
        kept["phase"] = kept["phase"][:32]
    instance.last_heartbeat_at = now
    instance.last_heartbeat = kept
    session.flush()
    wants_sync = instance.stop_requested_at is not None and (
        instance.last_synced_at is None or instance.last_synced_at < instance.stop_requested_at
    )
    return HeartbeatReply(stop=instance.job.state not in ACTIVE_JOB_STATES, sync=wants_sync)


def synced(session: Session, instance: ComputeInstance, *, now: datetime) -> None:
    """The runner saved /workspace. Once Krater asked for it, the scheduler stops the box on its next pass."""
    instance.last_synced_at = now
    _event(session, instance, "synced", "The box saved /workspace.", now=now)
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


def _prefix(instance: ComputeInstance, area: str) -> str:
    if area not in AREAS:
        raise ValidationFailed({"area": f"area must be one of {', '.join(AREAS)}"})
    job = instance.job
    return f"projects/{job.project_id}/jobs/{job.id}/{area}/"


def _key(instance: ComputeInstance, area: str, name: str) -> str:
    segments = name.split("/")
    if not _NAME_RE.match(name) or any(s in ("", ".", "..") for s in segments) or name != name.strip():
        raise ValidationFailed(
            {"name": "Use a relative path of letters, digits, spaces and . _ - = + , @ ~, with '/' between folders."}
        )
    return _prefix(instance, area) + name


def list_files(store: ObjectStore, instance: ComputeInstance, area: str) -> list[dict[str, Any]]:
    prefix = _prefix(instance, area)
    return [
        {
            "name": item.key[len(prefix) :],
            "size": item.size_bytes,
            "url": store.presign_download(item.key, expires=URL_EXPIRES_SECONDS),
        }
        for item in store.list_prefix(prefix)
    ]


def project_usage_bytes(store: ObjectStore, instance: ComputeInstance) -> dict[str, int]:
    """Bytes stored per key under the instance's project."""
    prefix = f"projects/{instance.job.project_id}/"
    return {item.key: item.size_bytes for item in store.list_prefix(prefix)}


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
    key = _key(instance, area, name)
    if not 0 <= size <= MAX_UPLOAD_BYTES:
        raise ValidationFailed({"size": f"Files can be up to {MAX_UPLOAD_BYTES // 1024**3} GB."})
    project_id = instance.job.project_id
    session.execute(sa.select(Project.id).where(Project.id == project_id).with_for_update())
    session.execute(
        sa.delete(StorageReservation).where(
            StorageReservation.project_id == project_id, StorageReservation.expires_at <= now
        )
    )
    usage = project_usage_bytes(store, instance)
    reserved = session.execute(
        sa.select(StorageReservation.key, StorageReservation.size_bytes).where(
            StorageReservation.project_id == project_id
        )
    )
    for reserved_key, reserved_size in reserved:
        usage[reserved_key] = max(usage.get(reserved_key, 0), reserved_size)
    used = sum(usage.values()) - usage.get(key, 0)
    if used + size > quota_bytes:
        raise ValidationFailed(
            {"size": f"The project's storage quota of {quota_bytes // 1024**3} GB is full ({used} bytes used)."}
        )
    reservation = session.get(StorageReservation, key)
    if reservation is None:
        reservation = StorageReservation(key=key, project_id=project_id)
        session.add(reservation)
    reservation.size_bytes = size
    reservation.expires_at = now + timedelta(seconds=URL_EXPIRES_SECONDS)
    session.flush()
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


def complete_upload(
    store: ObjectStore, instance: ComputeInstance, area: str, name: str, upload_id: str, etags: list[str]
) -> None:
    key = _key(instance, area, name)
    if not upload_id or not etags or not all(isinstance(e, str) and e for e in etags):
        raise ValidationFailed({"etags": "Send the ETag of every part, in order."})
    store.complete_multipart_upload(key, upload_id=upload_id, parts=list(enumerate(etags, start=1)))
