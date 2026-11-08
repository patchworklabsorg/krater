"""Admins' SSH access to every box.

Every Krater admin's SSH keys go on every box, next to the owner's, with nothing to request. Who is an admin comes
from Weave: the worker's `box_admins_sync` task (`sync_admins`) copies Weave's admin list into `BoxAdmin` every five
minutes, and the runner gets the whole key list when it starts and in every heartbeat reply
(`krater.compute.runner_api`), which reads only that table. A runner that reports `key_updates` (runners from Nov 7,
2026 on) rewrites authorized_keys when the list changes, so an admin added or removed in Weave, or a key an admin
adds or removes, reaches a running box within a sync and a heartbeat (about six minutes). An older runner reads its
keys only when it starts.

If Weave can't be reached the last list stays, but only for `ROSTER_STALE`: after that no admin's keys are sent, so
authorization still fails closed. What this doesn't do, and the job page and the new-box form say so:

- it doesn't end an SSH session that is already open: sshd checks keys only at login;
- an open session counts as a session, so the box doesn't stop for being idle while an admin is logged in, and keeps
  spending the project's budget;
- a shell on the box can read the project's secrets, as the owner's can;
- Krater doesn't know when an admin logs in: the runner reports only how many sessions are open.

Changes to the admin list write an `AuditEvent` (`AUDIT_ADMINS_CHANGED`), the record of who could reach boxes when.
Batch jobs have no sshd, so this is boxes only. `AdminBoxAccess` rows are the earlier request-with-a-reason grants,
kept as history; nothing writes them any more.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import BoxAdmin, ComputeInstance, ComputeJob, User, UserSshKey
from krater.services import audit, ssh_keys
from krater.services.actor import GROUP_ADMIN
from krater.weave import WeaveClient

logger = logging.getLogger(__name__)

#: How long Weave's last admin list counts when the sync can't reach Weave.
ROSTER_STALE = timedelta(hours=1)

AUDIT_ADMINS_CHANGED = "box_admins_changed"


def live_instance(job: ComputeJob) -> ComputeInstance | None:
    live = [row for row in job.instances if row.is_live]
    return live[-1] if live else None


def runner_takes_key_updates(instance: ComputeInstance | None) -> bool:
    """Whether the box's runner applies a key list from a heartbeat reply (it says so in its heartbeats)."""
    return instance is not None and bool((instance.last_heartbeat or {}).get("key_updates"))


def sync_admins(session: Session, weave_client: WeaveClient, *, now: datetime) -> tuple[list[str], list[str]]:
    """Replace `BoxAdmin` with the active users Weave says are admins and who have signed in to Krater (anyone else
    has no SSH keys here). Returns the Patchwork IDs (added, removed), and writes an `AuditEvent` when either is
    non-empty. Raises `WeaveUnavailableError` when Weave can't answer; the rows are then left as they were."""
    subs = {record.sub for record in weave_client.list_users_with_role(GROUP_ADMIN) if record.active}
    users = list(session.scalars(sa.select(User).where(User.weave_sub.in_(subs)))) if subs else []
    wanted = {user.id: user for user in users}
    rows = {row.user_id: row for row in session.scalars(sa.select(BoxAdmin).with_for_update())}

    removed = []
    for user_id, row in rows.items():
        if user_id not in wanted:
            removed.append(row.user.weave_sub)
            session.delete(row)
    added = []
    for user_id, user in wanted.items():
        row = rows.get(user_id)
        if row is None:
            session.add(BoxAdmin(user_id=user_id, synced_at=now))
            added.append(user.weave_sub)
        else:
            if row.synced_at <= now - ROSTER_STALE:
                added.append(user.weave_sub)  # its keys had stopped going out
            row.synced_at = now
    session.flush()
    added.sort()
    removed.sort()
    if added or removed:
        audit.record(session, None, AUDIT_ADMINS_CHANGED, payload={"added": added, "removed": removed})
        logger.info("krater box admins: added %s, removed %s", added, removed)
    return added, removed


def admin_ids(session: Session, *, now: datetime) -> list[uuid.UUID]:
    """The users whose keys go on every box now: the admins Weave confirmed within `ROSTER_STALE`."""
    return list(
        session.scalars(
            sa.select(BoxAdmin.user_id).where(BoxAdmin.synced_at > now - ROSTER_STALE).order_by(BoxAdmin.user_id)
        )
    )


def admins(session: Session, *, now: datetime) -> list[User]:
    """The `User`s of `admin_ids`, for the job page."""
    ids = admin_ids(session, now=now)
    return list(session.scalars(sa.select(User).where(User.id.in_(ids)))) if ids else []


def authorized_keys(session: Session, job: ComputeJob, *, now: datetime) -> list[str]:
    """Every line for a box's authorized_keys: the owner's keys, then every admin's (`admin_ids`). Keys are read
    fresh, so a key someone removes leaves the box too. Duplicates (an admin who owns the box) appear once."""
    lines = ssh_keys.authorized_keys_for(session, job.submitter_id)
    ids = set(admin_ids(session, now=now)) - {job.submitter_id}
    if ids:
        for key in session.scalars(
            sa.select(UserSshKey).where(UserSshKey.user_id.in_(ids)).order_by(UserSshKey.created_at)
        ):
            if key.authorized_keys_line not in lines:
                lines.append(key.authorized_keys_line)
    return lines
