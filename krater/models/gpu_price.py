"""GpuPrice: one aggregated row of Vast GPU pricing, per (accelerator name, accelerator count).

Replaced wholesale on every refresh (`krater.services.pricing.refresh_prices`) -- there's no history
here, just the latest known prices. See `docs/dev/pricing.md` for where the numbers come from and how
they're aggregated.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import UUIDPrimaryKeyMixin


class GpuPrice(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "gpu_prices"
    __table_args__ = (
        sa.UniqueConstraint("accelerator_name", "accelerator_count", name="uq_gpu_prices_accelerator_name"),
    )

    accelerator_name: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    accelerator_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # Typical machine shape for this group (median across its offers) -- `None` when the source didn't
    # carry the figure for any offer in the group. See `krater.services.pricing.aggregate_offers`.
    vram_gib: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    vcpus_typical: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    memory_gib_typical: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    on_demand_min_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    on_demand_median_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # `None` when no offer in the group quoted a (positive) spot price.
    spot_min_cents: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    offer_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<GpuPrice {self.accelerator_name}x{self.accelerator_count} median={self.on_demand_median_cents}c>"
