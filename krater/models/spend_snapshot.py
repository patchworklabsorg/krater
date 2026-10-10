"""SpendSnapshot: an estimated-spend reading written by the SkyPilot spend reconciler."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import SpendSource, pg_enum
from krater.models.mixins import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project


class SpendSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "spend_snapshots"
    __table_args__ = (sa.Index("ix_spend_snapshots_project_id_taken_at", "project_id", "taken_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False)
    estimated_spend_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    source: Mapped[SpendSource] = mapped_column(pg_enum(SpendSource, name="spend_source"), nullable=False)
    # Set in Python, not by the server default alone: Postgres `now()` is frozen for the whole transaction, so snapshots
    # written together (as the reconciler does) would tie and "latest" would be arbitrary.
    taken_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=sa.func.now()
    )

    project: Mapped[Project] = relationship(back_populates="spend_snapshots")

    def __repr__(self) -> str:
        return f"<SpendSnapshot project_id={self.project_id} estimated_spend_cents={self.estimated_spend_cents}>"
