"""What the worker watches outside the database for admins: the provider account's credit, how much of Krater's
storage each project uses, and deleting a finished project's files on the date it was given.

**Credit.** Every hour the worker asks the provider for the account's credit (`record_credit`); admins see it on
`/admin/compute/overview`, and `krater.services.ops_alerts` alerts when it falls under the low credit alert
(`ComputeSettings.credit_alert_cents`, off by default).

**Storage use.** Every hour the worker lists both buckets (`record_storage`): each project's checkpoints and outputs,
and the images (screenshots, gallery) that share the same store. These are stored bytes, what members are charged
against their quota. The store's disk use is higher: SeaweedFS frees a deleted file's space only when it compacts a
volume. `KRATER_COMPUTE_STORE_CAPACITY_GB` (0 = unknown, the default) says how big the store is, for the overview
and the "storage nearly full" alert.

**Finished projects' files.** When a project is completed or withdrawn it gets a date (`schedule_file_deletion`):
`ComputeSettings.finished_files_days` after it finished, fixed then, and posted in its channel with the archive
notice while the channel is still open. On that date the worker deletes everything under the project's prefix in the
compute bucket (`delete_due_files`). Never for a project with no date (one that finished before this existed, or
while the setting was 0), and never for one that isn't completed or withdrawn any more. Images are never deleted:
the gallery and the proposals use them.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeJob, Project, ProjectStatus, ProviderAccount, StorageUsage
from krater.services import audit, compute_settings
from krater.storage import ObjectStore

logger = logging.getLogger(__name__)

AUDIT_FILES_DELETED = "compute_files_deleted"
#: The store is "nearly full" from this share of its capacity.
STORE_ALERT_PERCENT = 80
#: Usage scopes that aren't one project's.
SCOPE_IMAGES = "images"
SCOPE_COMPUTE_OTHER = "compute-other"
MICROUSD_PER_USD = 1_000_000
_FINISHED = (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)


# -- Credit --------------------------------------------------------------------------------------------------------


def record_credit(session: Session, provider: str, credit_usd: Decimal | None, *, now: datetime) -> None:
    """Keep the provider's latest reported credit. `None` (the provider didn't say) keeps the last reading. The caller
    commits."""
    if credit_usd is None:
        return
    microusd = int((credit_usd * MICROUSD_PER_USD).to_integral_value())
    row = session.get(ProviderAccount, provider)
    if row is None:
        session.add(ProviderAccount(provider=provider, credit_microusd=microusd, observed_at=now))
    else:
        row.credit_microusd, row.observed_at = microusd, now
    session.flush()


def latest_credit(session: Session) -> ProviderAccount | None:
    return session.scalar(sa.select(ProviderAccount).order_by(ProviderAccount.observed_at.desc()).limit(1))


# -- Storage use ---------------------------------------------------------------------------------------------------


def record_storage(
    session: Session, compute_store: ObjectStore, image_store: ObjectStore | None, *, now: datetime
) -> int:
    """Count every object in the compute bucket by project, and the image bucket as one scope, replacing the last
    count. Returns the total bytes. The caller commits."""
    known = set(session.scalars(sa.select(Project.id)))
    counts: dict[str, list] = {}  # scope -> [project_id, bytes, objects]

    def add(scope: str, project_id: uuid.UUID | None, size: int) -> None:
        entry = counts.setdefault(scope, [project_id, 0, 0])
        entry[1] += size
        entry[2] += 1

    for item in compute_store.list_prefix(""):
        parts = item.key.split("/")
        project_id = None
        if len(parts) > 2 and parts[0] == "projects":
            try:
                project_id = uuid.UUID(parts[1])
            except ValueError:
                project_id = None
        if project_id is not None and project_id in known:
            add(f"project:{project_id}", project_id, item.size_bytes)
        else:
            add(SCOPE_COMPUTE_OTHER, None, item.size_bytes)
    if image_store is not None:
        for item in image_store.list_prefix(""):
            add(SCOPE_IMAGES, None, item.size_bytes)

    session.execute(sa.delete(StorageUsage))
    for scope, (project_id, size, objects) in counts.items():
        session.add(StorageUsage(scope=scope, project_id=project_id, bytes=size, objects=objects, observed_at=now))
    session.flush()
    return sum(entry[1] for entry in counts.values())


def storage_by_project(session: Session) -> dict[uuid.UUID, int]:
    rows = session.execute(
        sa.select(StorageUsage.project_id, StorageUsage.bytes).where(StorageUsage.project_id.isnot(None))
    )
    return {project_id: int(size) for project_id, size in rows}


def storage_totals(session: Session) -> tuple[int, int, datetime | None]:
    """(all bytes, image bytes, when counted) from the last count."""
    rows = session.scalars(sa.select(StorageUsage)).all()
    total = sum(row.bytes for row in rows)
    images = sum(row.bytes for row in rows if row.scope == SCOPE_IMAGES)
    observed = max((row.observed_at for row in rows), default=None)
    return total, images, observed


# -- Finished projects' files --------------------------------------------------------------------------------------


def schedule_file_deletion(session: Session, project: Project, *, finished_at: datetime) -> None:
    """Give a project that just finished its file deletion date, from the setting now. 0 days: none, its files are
    kept. The caller sets `finished_at` and commits."""
    days = compute_settings.org_limits(session).finished_files_days
    project.files_delete_on = finished_at + timedelta(days=days) if days > 0 else None


def has_compute_files(session: Session, project: Project) -> bool:
    """Whether the project ever ran compute, so its notices mention its files."""
    return (
        session.scalar(sa.select(sa.func.count()).select_from(ComputeJob).where(ComputeJob.project_id == project.id))
        > 0
    )


def delete_due_files(session: Session, store: ObjectStore, *, now: datetime) -> list[uuid.UUID]:
    """Delete the checkpoints, outputs, logs and workspaces of every finished project whose date has come, and any
    upload of theirs left unfinished. Commits after each project, so one storage error leaves the others done; the
    failed one is tried again on the next run."""
    stmt = sa.select(Project).where(
        Project.status.in_(_FINISHED),
        Project.files_delete_on.isnot(None),
        Project.files_delete_on <= now,
        Project.files_deleted_at.is_(None),
    )
    done = []
    for project in list(session.scalars(stmt)):
        prefix = f"projects/{project.id}/"
        objects = store.list_prefix(prefix)
        for item in objects:
            store.delete(item.key)
        for upload in store.list_multipart_uploads(prefix):
            store.abort_multipart_upload(upload.key, upload_id=upload.upload_id)
        project.files_deleted_at = now
        audit.record(
            session,
            None,
            AUDIT_FILES_DELETED,
            project=project,
            payload={
                "objects": len(objects),
                "bytes": sum(item.size_bytes for item in objects),
                "delete_on": project.files_delete_on.isoformat(),
            },
        )
        session.execute(sa.delete(StorageUsage).where(StorageUsage.project_id == project.id))
        session.commit()
        logger.info("krater.compute: deleted %d files of finished project %s", len(objects), project.id)
        done.append(project.id)
    return done
