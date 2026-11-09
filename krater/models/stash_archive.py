"""StashArchive: one URL of a submission that Krater asks Stash (the Patchwork archive) to keep a copy of.

A row is written in the same transaction as the submission that carries the URL, and the worker sends the
request later (`krater.stash.sender`). One row per project and URL: Stash is idempotent on the same
submission and URL, so a resubmission with the same link adds nothing. See `docs/stash-integration.md`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import StashArchiveState, pg_enum
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project

#: The archive statuses that mean "nothing more will change": Stash's `done`, `partial` and `failed`, and
#: Krater's own `missing`, set when Stash answers 404 (an admin deleted the archive). See `docs/stash-integration.md`.
FINAL_ARCHIVE_STATUSES = frozenset({"done", "partial", "failed", "missing"})

#: The problem codes the member who submitted the link can act on. The others are Stash's or the site's.
SUBMITTER_PROBLEM_CODES = frozenset({"not_public", "not_found"})


class StashArchive(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stash_archives"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "url", name="uq_stash_archives_project_id_url"),
        sa.Index("ix_stash_archives_state_seq", "state", "seq"),
    )

    # The order rows were written in: rows of one transaction share `created_at`, so the sender and the page
    # order by this instead (as `quilt_outbox.seq`).
    seq: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False, index=True
    )
    # The revision whose submission first carried the URL, for the page. Null when the revision is gone.
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project_revisions.id"), nullable=True
    )
    url: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    # Which field of the submission the URL came from: `repo_url` or `demo_url`.
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    state: Mapped[StashArchiveState] = mapped_column(
        pg_enum(StashArchiveState, name="stash_archive_state"),
        nullable=False,
        default=StashArchiveState.PENDING,
        server_default=StashArchiveState.PENDING.value,
    )
    # What Stash answered: its archive id (`arc_...`), the page a reviewer opens, and the archive's own status
    # (`queued`, `running`, `done`, `partial`, `failed`), refreshed by the worker until it is final.
    archive_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    page_url: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    archive_status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    archive_completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Stash's `problem` for a `partial` or `failed` archive: the failed capture that matters most to the
    # submitter. `problem_code` is one of Stash's error codes (`not_public`, `not_found`, `blocked`, `too_large`,
    # `timeout`, `tool_failed`, `unexpected`), `problem_hint` the sentence for a person, `problem_message` the
    # detail for whoever debugs. All null while nothing has failed.
    problem_code: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    problem_hint: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    problem_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # When the sender first asked Stash, for the backlog alert (`krater.services.ops_alerts`).
    first_attempted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    # `None` means "send it on the next run". Set after a failed attempt, for the backoff.
    next_attempt_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The HTTP status of the last attempt (`None`: no attempt yet, or no answer).
    last_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    project: Mapped[Project] = relationship(foreign_keys=[project_id])

    @property
    def archive_final(self) -> bool:
        """Whether Stash has finished with the archive (no more status changes to fetch)."""
        return self.archive_status in FINAL_ARCHIVE_STATUSES

    @property
    def submitter_can_fix(self) -> bool:
        """Whether the problem is one the member can fix: a private source, or a link that leads nowhere."""
        return self.problem_code in SUBMITTER_PROBLEM_CODES

    def __repr__(self) -> str:
        return f"<StashArchive project_id={self.project_id} url={self.url!r} state={self.state.value}>"
