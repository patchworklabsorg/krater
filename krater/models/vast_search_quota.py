"""The latest Vast search quota the worker has seen (one row), so admins can see it: the portal never talks to Vast."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base

#: The one row's id.
QUOTA_ID = 1


class VastSearchQuota(Base):
    """Vast's daily quota of search rows as of `observed_at` (`krater.compute.search_quota`)."""

    __tablename__ = "vast_search_quota"
    __table_args__ = (sa.CheckConstraint(f"id = {QUOTA_ID}", name="singleton"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, default=QUOTA_ID)
    limit_rows: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    remaining_rows: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    reset_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # The reset each alert was written for, so each is written once a day.
    low_alerted_reset_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    exhausted_alerted_reset_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
