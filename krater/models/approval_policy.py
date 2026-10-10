"""ApprovalPolicy: configuration, not code. What a review stage requires to be considered passed."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.enums import ApprovalStage, pg_enum
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ApprovalPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_policies"

    stage: Mapped[ApprovalStage] = mapped_column(pg_enum(ApprovalStage, name="approval_stage"), nullable=False)
    # Nullable: a policy with no `min_budget_cents` floor applies regardless of requested budget size.
    min_budget_cents: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    min_approvals: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default="1")
    # e.g. "ganymede:reviewer:senior"; nullable when any reviewer satisfies the policy.
    required_group: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<ApprovalPolicy stage={self.stage.value} min_approvals={self.min_approvals}>"
