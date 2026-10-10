"""Project: a proposal, its lifecycle status, and pointers back at its revisions."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import ProjectStatus, pg_enum
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.budget_entry import BudgetEntry
    from krater.models.project_revision import ProjectRevision
    from krater.models.spend_snapshot import SpendSnapshot
    from krater.models.user import User


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    submitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    status: Mapped[ProjectStatus] = mapped_column(
        pg_enum(ProjectStatus, name="project_status"),
        nullable=False,
        default=ProjectStatus.DRAFT,
        server_default=ProjectStatus.DRAFT.value,
    )
    # `current_revision_id`/`approved_revision_id` point at `project_revisions`, which itself has a
    # non-nullable FK back to `projects`. That's a genuine circular dependency between the two tables,
    # so these two are added via ALTER TABLE (`use_alter=True`) after both tables exist, and the
    # matching relationships below use `post_update=True` so the ORM sets them with a separate UPDATE
    # after both rows are inserted rather than during the INSERT itself.
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("project_revisions.id", use_alter=True),
        nullable=True,
    )
    approved_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("project_revisions.id", use_alter=True),
        nullable=True,
    )
    repo_url: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    slack_channel_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # Set once the channel has been archived (on `completed`/`withdrawn`; see `krater.services.slack_notify`).
    # `slack_channel_id` is kept even after archiving, so the project page can still link to it.
    slack_channel_archived: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    skypilot_workspace: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # The `allowed_users` Krater last sent to the workspace, so the reconciler can tell who it removed.
    # `None` until the first update after this column was added (the workspace then holds the whole team).
    skypilot_allowed_users: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    submitter: Mapped[User] = relationship(foreign_keys=[submitter_id], back_populates="projects")
    current_revision: Mapped[ProjectRevision | None] = relationship(
        foreign_keys=[current_revision_id], post_update=True
    )
    approved_revision: Mapped[ProjectRevision | None] = relationship(
        foreign_keys=[approved_revision_id], post_update=True
    )
    revisions: Mapped[list[ProjectRevision]] = relationship(
        foreign_keys="ProjectRevision.project_id", back_populates="project"
    )
    budget_entries: Mapped[list[BudgetEntry]] = relationship(back_populates="project")
    spend_snapshots: Mapped[list[SpendSnapshot]] = relationship(back_populates="project")

    def __repr__(self) -> str:
        return f"<Project {self.title!r} status={self.status.value}>"
