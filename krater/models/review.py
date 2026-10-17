"""Review: a single reviewer's decision on a project revision."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import ReviewDecision, ReviewSource, pg_enum
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project_revision import ProjectRevision
    from krater.models.user import User


class Review(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "reviews"
    # One review per reviewer per revision. Also enforced in `record_review` (via a pre-check, for a
    # clean error message in the common case), but the constraint is what actually prevents two
    # concurrent requests (a double-click, a retried Slack action) from both passing that check and
    # inserting a second row.
    __table_args__ = (sa.UniqueConstraint("revision_id", "reviewer_id", name="uq_reviews_revision_id_reviewer_id"),)

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project_revisions.id"), nullable=False, index=True
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    decision: Mapped[ReviewDecision] = mapped_column(pg_enum(ReviewDecision, name="review_decision"), nullable=False)
    # Required on reject; enforced by the review service, not a DB constraint.
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    source: Mapped[ReviewSource] = mapped_column(pg_enum(ReviewSource, name="review_source"), nullable=False)
    # Snapshot of the reviewer's roles (from Weave) *at review time*, so a later change to their roles (or to
    # an ApprovalPolicy's `required_group`) can't retroactively change whether a past review counts.
    reviewer_groups: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String), nullable=False, default=list, server_default="{}"
    )

    revision: Mapped[ProjectRevision] = relationship(back_populates="reviews")
    reviewer: Mapped[User] = relationship(back_populates="reviews")

    def __repr__(self) -> str:
        return f"<Review revision_id={self.revision_id} decision={self.decision.value}>"
