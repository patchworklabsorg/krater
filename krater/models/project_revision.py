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
    # The application's answers (docs/SPEC.md "The application"). `write_up` is the full explanation: what the
    # project is, what it builds on or departs from, and why it matters. Word limits are checked on submit.
    summary: Mapped[str] = mapped_column(sa.String(300), nullable=False, default="", server_default="")
    experience: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    write_up: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # `none`, `contest`, `professor` or `both` (krater.services.projects.AFFILIATIONS), with the contest's or
    # professor's name and any link in `affiliation_detail`.
    affiliation: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="none", server_default="none")
    affiliation_detail: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    compute_use: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")

    # What approval funds: the proof of concept for a proposal, the new total for an amendment.
    budget_requested_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # The estimate behind `budget_requested_cents`: the card, hours and rental the team chose, priced by the server
    # (`krater.services.pricing.estimate_cost`), or at the hourly price the team typed in instead (`rate_source`
    # `member`, with Krater's own `market_rate_cents` beside it when it has one). The team may still request a
    # different amount; the project page shows reviewers both. Null on revisions from before the application form.
    budget_estimate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # The finished product's rough cost, for reviewers to see where the project is heading. Information only:
    # approval never funds it (that takes an amendment later), so it never feeds the budget ledger.
    full_budget_cents: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    full_budget_estimate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Completion-only fields; left empty/null for `proposal` and `amendment` revisions.
    demo_url: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    screenshot_keys: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String), nullable=False, default=list, server_default="{}"
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
