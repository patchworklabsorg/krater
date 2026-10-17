"""GpuPriceSample: Krater's own Vast price history, one row per (GPU model, rental type, sample time).

Vast offers no price history to client accounts (its market metrics API answers host accounts only, and bulk use
of its public price feed needs a data license), so Krater records its own. `krater.services.price_history`
writes a row every `price_history_interval_minutes` for each tracked GPU model and rental type.

Prices are full hourly costs as Krater compares offers: the GPU price (the minimum bid for `bid`, `dph_base` for
`ondemand`) plus `disk_gb` of the host's storage, in integer micro-dollars per hour. Bandwidth is per GB used and
isn't included. A sample with no offers has `offer_count = 0` and no prices.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import UUIDPrimaryKeyMixin


class GpuPriceSample(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "gpu_price_samples"
    __table_args__ = (
        sa.CheckConstraint("rental_type IN ('bid', 'ondemand')", name="rental_type"),
        sa.Index("ix_gpu_price_samples_series", "gpu_name", "rental_type", "num_gpus", "sampled_at"),
        sa.Index("ix_gpu_price_samples_sampled_at", "sampled_at"),
    )

    sampled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    gpu_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    rental_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    num_gpus: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The disk size the prices include (the basis disk, `price_history_disk_gb` when sampled).
    disk_gb: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    offer_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # True when the search hit Vast's 512-offer cap, so the p10 and median are of the cheapest 512 only.
    truncated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    min_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    p10_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    median_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<GpuPriceSample {self.gpu_name} {self.rental_type} x{self.num_gpus} at {self.sampled_at:%Y-%m-%d %H:%M} "
            f"min={self.min_microusd}>"
        )
