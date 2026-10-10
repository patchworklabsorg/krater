"""AuditEvent: an immutable record of an admin override -- who, what, when and why."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project
    from krater.models.user import User


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_events"

    # Nullable: the SkyPilot reconciler (`krater.services.skypilot_sync`) writes audit events as a
    # system actor with no `User` behind it (there's no human to attribute a periodic reconcile run
    # to). Every human-triggered action still always supplies an actor.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True, index=True
    )
    # Open-ended (e.g. "admin_approve", "budget_adjust", "launch_blocked") per SPEC.md -- not an enum,
    # since new admin actions shouldn't need a migration to be logged.
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    actor: Mapped[User | None] = relationship(back_populates="audit_events")
    project: Mapped[Project | None] = relationship()

    def __repr__(self) -> str:
        return f"<AuditEvent action={self.action!r} project_id={self.project_id}>"
