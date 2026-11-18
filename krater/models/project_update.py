"""A progress update a project's team posts, and a top-up request when it asks for more budget.

Updates are how a team tells reviewers what the budget so far bought (`krater.services.project_updates`). The team,
reviewers and admins see them on the project page, and Krater posts each one to the project's Slack channel.

A top-up request is an update with an amount: Krater opens and submits an amendment for it (`revision_id`) that adds
that amount to the ceiling, so it is reviewed and granted exactly like any amendment. The update carries the reason
and the progress the reviewers read; the amendment carries the application, unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project_revision import ProjectRevision
    from krater.models.user import User


class ProjectUpdate(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "project_updates"
    __table_args__ = (sa.Index("ix_project_updates_project_id_created_at", "project_id", "created_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False)
    # Who posted it, by Krater user id (their Patchwork ID is on the user).
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    # What the team has done, in Markdown (`krater.web.textfmt.markdown`). Empty only on a top-up request.
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    # A top-up request: how much more the team asks for, and why. Both null on a plain update.
    topup_cents: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    topup_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # The amendment the top-up request opened, which reviewers decide.
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project_revisions.id"), nullable=True, unique=True
    )
    # The budget when it was posted, so readers see what the update is measured against.
    ceiling_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    spent_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The jobs and boxes started since the team's previous update (or since approval, for the first).
    jobs_since: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    # When it was posted to the project's Slack channel (or skipped: no channel, or an archived one), and the message.
    slack_posted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    slack_message_ts: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    author: Mapped[User] = relationship(foreign_keys=[author_id])
    revision: Mapped[ProjectRevision | None] = relationship(foreign_keys=[revision_id])

    @property
    def is_topup(self) -> bool:
        return self.topup_cents is not None

    def __repr__(self) -> str:
        return f"<ProjectUpdate {self.id} project={self.project_id} topup={self.topup_cents}>"
