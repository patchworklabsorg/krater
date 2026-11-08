"""Which URLs of a submission Krater asks Stash to archive, and what the project page says about each.

A submission carries up to two links: the project's repository (`Project.repo_url`) and, on a completion
revision, the demo (`ProjectRevision.demo_url`). `sync_project` writes one `stash_archives` row per project and
URL, in the caller's transaction, so a row exists exactly when the submission that carries the URL commits. The
worker asks Stash later (`krater.stash.sender`); a member's request never waits on Stash, and a submission never
fails because Stash is down. See `docs/stash-integration.md`.

Because it only adds rows for URLs the project hasn't got yet, calling it again changes nothing, and a
resubmission with the same link adds nothing (Stash is idempotent on the same submission and URL anyway).
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from krater.models import Project, ProjectRevision, RevisionKind, StashArchive, StashArchiveState

SOURCE_REPO = "repo_url"
SOURCE_DEMO = "demo_url"


def submission_urls(project: Project) -> list[tuple[str, str, ProjectRevision | None]]:
    """The `(source, url, revision)` links of the project's submitted state: the repository link, and the demo
    link of the current revision when that revision is a submitted completion. A draft's links wait until it is
    submitted, so a reviewer sees archives of what was actually sent in."""
    revision = project.current_revision
    submitted = revision is not None and revision.submitted_at is not None
    if not submitted:
        return []
    links: list[tuple[str, str, ProjectRevision | None]] = []
    if project.repo_url:
        links.append((SOURCE_REPO, project.repo_url, revision))
    if revision is not None and revision.kind is RevisionKind.COMPLETION and revision.demo_url:
        links.append((SOURCE_DEMO, revision.demo_url, revision))
    return links


def sync_project(session: Session, project: Project) -> int:
    """Add a pending `stash_archives` row for each submitted URL the project hasn't got a row for. Returns how
    many rows were added. Call it in the transaction that submits the revision."""
    links = submission_urls(project)
    if not links:
        return 0
    values = [
        {
            "project_id": project.id,
            "revision_id": revision.id if revision is not None else None,
            "url": url,
            "source": source,
            "state": StashArchiveState.PENDING,
        }
        for source, url, revision in links
    ]
    stmt = (
        insert(StashArchive)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_stash_archives_project_id_url")
        .returning(StashArchive.id)
    )
    return len(session.execute(stmt).all())


def for_project(session: Session, project: Project) -> list[StashArchive]:
    """The project's archive rows, oldest first."""
    stmt = sa.select(StashArchive).where(StashArchive.project_id == project.id).order_by(StashArchive.seq)
    return list(session.scalars(stmt))


def cancel_pending(session: Session, project: Project) -> int:
    """Mark the project's rows that Stash hasn't been asked about yet as `skipped`, so the sender never asks:
    nobody reviews a withdrawn project, so its links need no copy. Rows Stash already has stay. Returns how many
    rows were skipped. Call it in the transaction that withdraws the project."""
    stmt = (
        sa.update(StashArchive)
        .where(StashArchive.project_id == project.id, StashArchive.state == StashArchiveState.PENDING)
        .values(state=StashArchiveState.SKIPPED, next_attempt_at=None)
    )
    return session.execute(stmt).rowcount or 0


@dataclass(frozen=True)
class ArchiveDisplay:
    """One line on the project page: the link that was submitted, the Stash page (once there is one), and a
    short status with a badge tone (`good`, `info`, `bad`)."""

    url: str
    source: str
    page_url: str | None
    label: str
    tone: str


#: What each Stash archive status reads as on the page.
_STATUS_LABELS = {
    None: ("Archiving", "info"),
    "queued": ("Archiving", "info"),
    "running": ("Archiving", "info"),
    "done": ("Archived", "good"),
    "partial": ("Partly archived", "info"),
    "failed": ("Archive failed", "bad"),
    "missing": ("Archive removed", "bad"),
}


def display(row: StashArchive) -> ArchiveDisplay:
    """How the project page shows one row."""
    if row.state is StashArchiveState.PENDING:
        label, tone = "Archive pending", "info"
    elif row.state is StashArchiveState.FAILED:
        label, tone = "Not archivable", "bad"
    elif row.state is StashArchiveState.SKIPPED:
        label, tone = "Not archived", "info"
    else:
        label, tone = _STATUS_LABELS.get(row.archive_status, ("Archiving", "info"))
    return ArchiveDisplay(url=row.url, source=row.source, page_url=row.page_url, label=label, tone=tone)


__all__ = [
    "SOURCE_DEMO",
    "SOURCE_REPO",
    "ArchiveDisplay",
    "cancel_pending",
    "display",
    "for_project",
    "submission_urls",
    "sync_project",
]
