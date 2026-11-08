"""OSPool grants: projects OSPool funds outside Krater, and their usage as OSG's accounting reports it.

OSPool capacity is free, so an OSPool grant never touches the dollar ledger (`BudgetEntry`), and so never reaches
Quilt, which would fund it in cash. The team runs on OSPool with its own OSG accounts, under its own OSPool project
name; Krater only records the grant and reports the usage (`krater.services.ospool_grants`, docs/SPEC.md "OSPool
grants").
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project
    from krater.models.user import User


class OspoolGrant(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One project's OSPool grant. A project has at most one, and an OSPool project name belongs to one grant."""

    __tablename__ = "ospool_grants"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False, unique=True
    )
    #: The registered OSPool project name the team's jobs carry (`+ProjectName`), as OSG's accounting knows it.
    ospool_project_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    #: Usage counts from this day (UTC). It may be earlier than the day the grant was recorded.
    counted_from: Mapped[date] = mapped_column(sa.Date, nullable=False)
    #: The GPU the usage is valued as, by its name in Krater's price catalog. OSG's accounting doesn't say which
    #: GPU a job used.
    gpu_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: An admin's price per GPU-hour in micro-dollars (a neocloud's posted price, say), used instead of Krater's own.
    rate_override_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    #: Short, for the admin's note: say where the override's price comes from.
    rate_note: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="", server_default="")
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    last_imported_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_import_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    project: Mapped[Project] = relationship()
    actor: Mapped[User] = relationship()
    usage_days: Mapped[list[OspoolUsageDay]] = relationship(back_populates="grant", order_by="OspoolUsageDay.day")

    def __repr__(self) -> str:
        return f"<OspoolGrant {self.ospool_project_name} project_id={self.project_id}>"


class OspoolUsageDay(UUIDPrimaryKeyMixin, Base):
    """One UTC day of a grant's usage, as OSG's accounting summed it (user jobs only, not the pilots that ran them).

    OSG corrects recent days, so the import replaces a day's hours. Its price per GPU-hour is the one in force when
    the day was first imported, and stays: a day's value doesn't move with later prices.
    """

    __tablename__ = "ospool_usage_days"
    __table_args__ = (sa.UniqueConstraint("grant_id", "day", name="uq_ospool_usage_days_grant_day"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("ospool_grants.id"), nullable=False, index=True
    )
    day: Mapped[date] = mapped_column(sa.Date, nullable=False)
    gpu_hours: Mapped[Decimal] = mapped_column(sa.Numeric(14, 4), nullable=False)
    core_hours: Mapped[Decimal] = mapped_column(sa.Numeric(14, 4), nullable=False)
    jobs: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: The price per GPU-hour that values this day, in micro-dollars; `None` while Krater has no price for the GPU.
    rate_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    #: Where the price came from: `admin` (the grant's override), or
    #: `krater_samples` or `catalog` (`krater.services.pricing`'s sources).
    rate_source: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    #: `gpu_hours` at `rate_microusd`, in cents: what renting it would have cost. `None` without a price.
    value_cents: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    grant: Mapped[OspoolGrant] = relationship(back_populates="usage_days")

    def __repr__(self) -> str:
        return f"<OspoolUsageDay {self.day} gpu_hours={self.gpu_hours}>"
