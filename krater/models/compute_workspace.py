"""A box's saved /workspace, kept apart from any one box so a later box can continue it.

Every box has one (`ComputeJob.workspace_id`): a new one, or one an earlier box of the project saved, which the new
box continues (`krater.services.workspaces`). The files live in Krater's storage under `prefix`, with the manifest
the runner writes (`runner/workspace.go`). The prefix is the per-job path of the box that started the workspace
(`projects/<project>/jobs/<job>/workspace/`), so the files never move: an older release, which knows only per-job
paths, still finds every box's own workspace.

A workspace outlives its boxes. It goes when the team deletes it, or with the rest of the project's files once the
project is finished (`compute_watch.delete_due_files`). One box at a time may use it: a second would overwrite, and
prune, the first one's saves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ComputeWorkspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "compute_workspaces"
    __table_args__ = (sa.Index("ix_compute_workspaces_project_id_created_at", "project_id", "created_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False)
    # What the team calls it: the name of the box that started it, unless someone gave another.
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    # Who started it (the first box's submitter), by Krater user id.
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    # Where its files are stored, ending in "/". Unique: two workspaces never share files.
    prefix: Mapped[str] = mapped_column(sa.String(500), nullable=False, unique=True)
    # The box that started it, whose per-job path is the prefix.
    origin_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, unique=True)
    # The last save that stored everything, from any of its boxes (as of when that save began), and what it held.
    last_saved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    files: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    bytes: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    # When the team deleted it (its files are removed then). A deleted workspace is listed nowhere.
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<ComputeWorkspace {self.name!r} {self.prefix}>"
