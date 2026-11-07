"""HeldGrant: a budget grant approved while the team wasn't cleared in Weave, waiting to be written to the ledger.

`krater.services.grants` writes one instead of a `BudgetEntry` when a grant (a proposal approval, an amendment that
raises the ceiling, an admin increase) is decided before every team member passed ID verification and, under 18, has
a verified guardian. The release task writes the `BudgetEntry` once Weave clears the team; until then the ledger,
and so Quilt, has nothing. A project has at most one pending hold.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import BudgetEntryKind, pg_enum
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.budget_entry import BudgetEntry
    from krater.models.project import Project
    from krater.models.project_revision import ProjectRevision
    from krater.models.user import User


class HeldGrant(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "held_grants"
    __table_args__ = (
        # One pending hold per project: while it's pending, nothing else may change the project's budget.
        sa.Index(
            "uq_held_grants_one_pending",
            "project_id",
            unique=True,
            postgresql_where=sa.text("released_at IS NULL AND cancelled_at IS NULL"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False, index=True
    )
    # What the `BudgetEntry` will be: its kind, signed amount, who decided it (the approver or admin), and why.
    kind: Mapped[BudgetEntryKind] = mapped_column(pg_enum(BudgetEntryKind, name="budget_entry_kind"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project_revisions.id"), nullable=True
    )

    # Released: the ledger entry is written. Cancelled: the project was withdrawn or completed first.
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    budget_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("budget_entries.id"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    project: Mapped[Project] = relationship()
    actor: Mapped[User] = relationship()
    revision: Mapped[ProjectRevision | None] = relationship()
    budget_entry: Mapped[BudgetEntry | None] = relationship()

    @property
    def pending(self) -> bool:
        return self.released_at is None and self.cancelled_at is None

    def __repr__(self) -> str:
        return f"<HeldGrant project_id={self.project_id} kind={self.kind.value} amount_cents={self.amount_cents}>"
