"""BudgetEntry: an append-only ledger row. A project's budget ceiling is the sum of its entries."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import BudgetEntryKind, pg_enum
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project
    from krater.models.project_revision import ProjectRevision
    from krater.models.user import User


class BudgetEntry(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "budget_entries"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False, index=True
    )
    kind: Mapped[BudgetEntryKind] = mapped_column(pg_enum(BudgetEntryKind, name="budget_entry_kind"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project_revisions.id"), nullable=True, index=True
    )

    project: Mapped[Project] = relationship(back_populates="budget_entries")
    actor: Mapped[User] = relationship(back_populates="budget_entries")
    revision: Mapped[ProjectRevision | None] = relationship()

    def __repr__(self) -> str:
        return f"<BudgetEntry project_id={self.project_id} kind={self.kind.value} amount_cents={self.amount_cents}>"
