"""Admin SSH access to a running box (`AdminBoxAccess`).

An admin asks for access with a reason. Krater then adds that admin's SSH keys to the box's authorized_keys, next to
the owner's, for `ACCESS_HOURS` or until an admin revokes it. The runner gets the whole key list in every heartbeat
reply (`krater.compute.runner_api.heartbeat`) and rewrites authorized_keys when it changes, so a grant, its expiry and
its revocation take effect within a heartbeat (a minute). What this doesn't do, and the job page says so:

- it doesn't end an SSH session that is already open: sshd checks keys only at login;
- an open session counts as a session, so the box doesn't stop for being idle while an admin is logged in, and keeps
  spending the project's budget;
- a shell on the box can read the project's secrets, as the owner's can;
- the admin's role is checked in Weave when access is granted, not again on each heartbeat.

Only boxes whose runner reports `key_updates` (runners from Nov 7, 2026 on) apply a key list from a heartbeat; an
older box learns keys only when it starts, so access to it is refused.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AdminBoxAccess, ComputeInstance, ComputeJob, ComputeJobKind, ComputeJobState, UserSshKey
from krater.services import audit, ssh_keys
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed

ACCESS_HOURS = 2

AUDIT_GRANTED = "admin_box_access"
AUDIT_REVOKED = "admin_box_access_revoked"


def live_instance(job: ComputeJob) -> ComputeInstance | None:
    live = [row for row in job.instances if row.is_live]
    return live[-1] if live else None


def runner_takes_key_updates(instance: ComputeInstance | None) -> bool:
    """Whether the box's runner applies a key list from a heartbeat reply (it says so in its heartbeats)."""
    return instance is not None and bool((instance.last_heartbeat or {}).get("key_updates"))


def active(session: Session, job: ComputeJob, *, now: datetime) -> list[AdminBoxAccess]:
    """The grants on `job` that are in force now, soonest to end first."""
    return list(
        session.scalars(
            sa.select(AdminBoxAccess)
            .where(
                AdminBoxAccess.job_id == job.id,
                AdminBoxAccess.revoked_at.is_(None),
                AdminBoxAccess.expires_at > now,
            )
            .order_by(AdminBoxAccess.expires_at)
        )
    )


def authorized_keys(session: Session, job: ComputeJob, *, now: datetime) -> list[str]:
    """Every line for a box's authorized_keys: the owner's keys, then those of each admin with access now. An admin's
    keys are read fresh, so a key they remove leaves the box too. Duplicates (an admin who owns the box) appear
    once."""
    lines = ssh_keys.authorized_keys_for(session, job.submitter_id)
    admin_ids = {grant.admin_id for grant in active(session, job, now=now)} - {job.submitter_id}
    if admin_ids:
        for key in session.scalars(
            sa.select(UserSshKey).where(UserSshKey.user_id.in_(admin_ids)).order_by(UserSshKey.created_at)
        ):
            if key.authorized_keys_line not in lines:
                lines.append(key.authorized_keys_line)
    return lines


def grant(session: Session, actor: Actor, *, job: ComputeJob, reason: str, now: datetime) -> AdminBoxAccess:
    """Give `actor` (an admin, checked in Weave by the caller's `fresh_actor`) SSH access to the running box `job`
    for `ACCESS_HOURS`. Needs a reason and at least one SSH key on the admin's account. Writes an `AuditEvent`.

    Raises `NotAllowed` for a non-admin, `InvalidState` if `job` isn't a running box, its runner can't take key
    updates, or the admin already has access, and `ValidationFailed` without a reason or an SSH key."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin can get SSH access to a member's box.")
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required. The team sees it."})
    if job.kind is not ComputeJobKind.BOX or job.state is not ComputeJobState.RUNNING:
        raise InvalidState("SSH access is only for a box that is running.")
    if not runner_takes_key_updates(live_instance(job)):
        raise InvalidState(
            "This box's runner is older than live key updates, so it would only take your key if it restarted."
        )
    if not ssh_keys.authorized_keys_for(session, actor.user.id):
        raise ValidationFailed({"reason": "Add an SSH key to your account first (SSH keys, in the navigation)."})
    mine = [each for each in active(session, job, now=now) if each.admin_id == actor.user.id]
    if mine:
        raise InvalidState(f"You already have access until {mine[0].expires_at.strftime('%H:%M UTC')}.")

    access = AdminBoxAccess(
        job_id=job.id,
        admin_id=actor.user.id,
        reason=reason.strip(),
        expires_at=now + timedelta(hours=ACCESS_HOURS),
    )
    session.add(access)
    session.flush()
    audit.record(
        session,
        actor,
        AUDIT_GRANTED,
        project=job.project,
        payload={"job_id": str(job.id), "until": access.expires_at.isoformat(timespec="minutes")},
        reason=access.reason,
    )
    return access


def revoke(session: Session, actor: Actor, *, access_id: uuid.UUID, job: ComputeJob, now: datetime) -> AdminBoxAccess:
    """End a grant on `job` now. Any admin can. Writes an `AuditEvent`. Raises `NotFound` for a grant that isn't
    on `job`, and `InvalidState` for one that has already ended."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin can end an admin's access to a box.")
    access = session.get(AdminBoxAccess, access_id)
    if access is None or access.job_id != job.id:
        raise NotFound("No such access on this box.")
    if not access.is_active(now):
        raise InvalidState("That access has already ended.")
    access.revoked_at = now
    access.revoked_by_id = actor.user.id
    session.flush()
    audit.record(
        session,
        actor,
        AUDIT_REVOKED,
        project=job.project,
        payload={"job_id": str(job.id), "admin_sub": access.admin.weave_sub},
    )
    return access
