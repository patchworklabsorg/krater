"""ProjectMember: someone on a project's team besides its lead (the submitter), or invited to it."""

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
    from krater.models.project import Project
    from krater.models.user import User


class ProjectMember(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An invitation that becomes team membership once the invitee accepts (`accepted_at`).

    Only an accepted member gets the team's access (`krater.services.teams.on_team`). An invitee already counts
    as too close to the project to review it (`krater.services.teams.has_conflict`). A removed member's row is
    deleted; the audit log keeps the history.
    """

    __tablename__ = "project_members"
    __table_args__ = (sa.UniqueConstraint("project_id", "user_id", name="uq_project_members_project_id_user_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    invited_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id])

    @property
    def accepted(self) -> bool:
        return self.accepted_at is not None

    def __repr__(self) -> str:
        return f"<ProjectMember project_id={self.project_id} user_id={self.user_id} accepted={self.accepted}>"
