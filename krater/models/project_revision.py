"""ProjectRevision: an immutable snapshot of a project as submitted for review."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import RevisionKind, RevisionOutcome, pg_enum
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project
    from krater.models.review import Review


class ProjectRevision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "project_revisions"
    __table_args__ = (sa.UniqueConstraint("project_id", "number", name="uq_project_revisions_project_id_number"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    kind: Mapped[RevisionKind] = mapped_column(pg_enum(RevisionKind, name="revision_kind"), nullable=False)
    write_up: Mapped[str] = mapped_column(sa.Text, nullable=False)
    budget_requested_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # Set only when the submitter used the budget estimator (`krater.services.pricing`) to fill in
    # `budget_requested_cents` -- null for a plain hand-typed budget. Always the server's own
    # recomputed breakdown (see `krater.services.pricing.estimate_cost`), never anything a client sent
    # directly, so a reviewer can trust the rate/refreshed_at shown next to the requested budget.
    budget_estimate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Completion-only fields; left empty/null for `proposal` and `amendment` revisions.
    demo_url: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    screenshot_keys: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String), nullable=False, default=list, server_default="{}"
    )
    credited_builder_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False, default=list, server_default="{}")

    # Null until the revision is actually submitted for review (vs. a draft edited in place).
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # The review message posted for this revision (see `krater.services.slack_notify`), so a later
    # decision can update it in place rather than posting a new one. Both null until it's posted.
    slack_message_channel_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    slack_message_ts: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    outcome: Mapped[RevisionOutcome] = mapped_column(
        pg_enum(RevisionOutcome, name="revision_outcome"),
        nullable=False,
        default=RevisionOutcome.PENDING,
        server_default=RevisionOutcome.PENDING.value,
    )

    project: Mapped[Project] = relationship(foreign_keys=[project_id], back_populates="revisions")
    reviews: Mapped[list[Review]] = relationship(back_populates="revision")

    def __repr__(self) -> str:
        return f"<ProjectRevision project_id={self.project_id} number={self.number} kind={self.kind.value}>"
