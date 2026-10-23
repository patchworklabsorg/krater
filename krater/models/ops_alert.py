"""Problems the worker tells admins about in Slack (`krater.services.ops_alerts`)."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base


class OpsAlert(Base):
    """One kind of problem, by `key` (`task:quilt_deliver`, `quilt_backlog` ...): when it was last seen, whether it
    is open, and when admins were last told."""

    __tablename__ = "ops_alerts"

    key: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    # Failures seen since the window last came clean; an alert opens at the check's threshold.
    failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    opened_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    """When the worker last ran its every-minute heartbeat (one row): `/healthz/worker` reads it, since the web app
    and the worker are separate containers."""

    __tablename__ = "worker_heartbeat"
    __table_args__ = (sa.CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, default=1)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
