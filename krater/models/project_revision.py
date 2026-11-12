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
    # Whether anything would stop the team open-sourcing the project (the gallery shows open-source work only), and
    # if so what: a contest's or school's rules, data or a model they may not share, a license. The form's checkbox
    # starts at no; `open_source_obstacles` is asked, and kept, only while it's yes.
    open_source_blocked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default="false")
    open_source_obstacles: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    # Where the training data comes from: the Submission Criteria ask applicants to name their data sources.
    training_data: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    # When the applicant ticked "I've read the Ganymede Submission Criteria" on this revision. Not carried to the
    # next draft: every submission confirms it again.
    criteria_read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # What approval funds: the proof of concept for a proposal, the new total for an amendment.
    budget_requested_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # What the approval granted, when an approver chose another amount than the request, less or more (same meaning:
    # the proof of concept, or the amendment's new total). Null means the full request, which is every revision
    # approved in full and every one from before approvers could choose. Read it through `approved_budget_cents`.
    budget_approved_cents: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

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
    # The primary image: at most one key, which the gallery shows on its list and at the top of the page
    # (`krater.services.screenshots`). The images the write-up shows inline are `image_keys`.
    screenshot_keys: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String), nullable=False, default=list, server_default="{}"
    )
    image_keys: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False, default=list, server_default="{}")
    tags: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False, default=list, server_default="{}")

    # Null until the revision is actually submitted for review (vs. a draft edited in place).
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # The review message posted for this revision (see `krater.services.slack_notify`), so a later
    # decision can update it in place rather than posting a new one. Both null until it's posted.
    slack_message_channel_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    slack_message_ts: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    # When the review message is in the reviewers-only channel: the notice in the team's channel (the project's
    # `slack_channel_id`) that says it's in review, which the decision updates. Null when the review message is in
    # the team channel itself (every revision from before reviewer channels).
    slack_team_message_ts: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    outcome: Mapped[RevisionOutcome] = mapped_column(
        pg_enum(RevisionOutcome, name="revision_outcome"),
        nullable=False,
        default=RevisionOutcome.PENDING,
        server_default=RevisionOutcome.PENDING.value,
    )

    project: Mapped[Project] = relationship(foreign_keys=[project_id], back_populates="revisions")
    reviews: Mapped[list[Review]] = relationship(back_populates="revision")

    @property
    def approved_budget_cents(self) -> int:
        """What approving this revision funds: the amount the approvers chose, else the full request."""
        if self.budget_approved_cents is not None:
            return self.budget_approved_cents
        return self.budget_requested_cents

    @property
    def granted_less(self) -> bool:
        """Whether the approvers granted less than this revision asked for."""
        return self.budget_approved_cents is not None and self.budget_approved_cents < self.budget_requested_cents

    @property
    def granted_more(self) -> bool:
        """Whether the approvers granted more than this revision asked for."""
        return self.budget_approved_cents is not None and self.budget_approved_cents > self.budget_requested_cents

    @property
    def granted_other_amount(self) -> bool:
        """Whether the approvers granted another amount than this revision asked for, less or more."""
        return self.granted_less or self.granted_more

    def __repr__(self) -> str:
        return f"<ProjectRevision project_id={self.project_id} number={self.number} kind={self.kind.value}>"
