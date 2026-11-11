"""What Krater records to learn how the compute market behaves, beyond its own price history: image sizes (for pull
cost and time), how long its jobs wait for a machine at a given price, and how a host's minimum bid moves hour to
hour (for outbid risk). Nothing reads the last two yet: they are collected so wait-time forecasts and outbid-adjusted
prices can be built on real data."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import UUIDPrimaryKeyMixin


class ImageSize(Base):
    """An image's compressed linux/amd64 size by manifest digest (`krater.registry`), or why it couldn't be read."""

    __tablename__ = "image_sizes"

    digest: Mapped[str] = mapped_column(sa.String(80), primary_key=True)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ComputeWaitSample(UUIDPrimaryKeyMixin, Base):
    """One wait for a machine: from when the job started waiting (submitted, or requeued after losing a machine) to
    when Krater rented one or the job gave up, with the price it waited at and the market it last saw."""

    __tablename__ = "compute_wait_samples"
    __table_args__ = (sa.Index("ix_compute_wait_samples_ended_at", "ended_at"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("compute_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    gpu_names: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    num_gpus: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    min_gpu_ram_gb: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    rental_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # Full hourly prices, micro-dollars: the job's target and max, and the limit it searched under at the end.
    target_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    max_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    limit_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    waited_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # `rented` or `cancelled` (gave up at its max wait).
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # What it rented at, and the cheapest offer of its last search (full hourly cost).
    rented_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    cheapest_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)


class GpuOfferObservation(UUIDPrimaryKeyMixin, Base):
    """One host's price at one hourly price sample (`krater.services.price_history`): the cheapest few offers per GPU
    model and rental type. Following a machine's minimum bid from sample to sample shows how often bids on it rise,
    which is what outbids a running interruptible machine. Kept for `OBSERVATION_DAYS`."""

    __tablename__ = "gpu_offer_observations"
    __table_args__ = (
        sa.Index("ix_gpu_offer_observations_machine", "gpu_name", "rental_type", "machine_id", "sampled_at"),
        sa.Index("ix_gpu_offer_observations_sampled_at", "sampled_at"),
    )

    sampled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    gpu_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    rental_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    machine_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    offer_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # The GPU's own hourly price (minimum bid, or `dph_base` on demand) and the full hourly cost, micro-dollars.
    gpu_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    full_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reliability: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
