"""Live price checks: what Vast offers for a job's shape right now (`krater.services.live_prices`), and how many
searches they used each day. Kept apart from the hourly price history (`GpuPriceSample`), so its charts and the
"in the last day" figures stay one sample an hour."""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import UUIDPrimaryKeyMixin


class LiveQuote(UUIDPrimaryKeyMixin, Base):
    """The cheapest offers for one job shape, as Vast answered a live search. One row per shape (`key`, a hash of the
    shape), checked again when a member asks and the last check is stale."""

    __tablename__ = "live_quotes"

    key: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    # The shape: gpu_names, min_gpu_ram_gb, num_gpus, disk_gb, box, rental_type.
    shape: Mapped[dict] = mapped_column(JSONB, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The full hourly cost Krater would pay for the cheapest (bid margin included), or None when none qualified.
    cheapest_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    # Up to three of the cheapest: [{"gpu": ..., "full_microusd": ...}], cheapest first.
    offers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Why the last check didn't search (quota kept for the scheduler, Vast unavailable), for the member.
    error: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)


class LivePriceUsage(Base):
    """Searches live price checks made in a UTC day, by purpose: Krater's own cap, which holds even when Vast's quota
    headers are missing."""

    __tablename__ = "live_price_usage"

    day: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    purpose: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    searches: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
