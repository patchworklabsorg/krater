"""add gpu price samples

Revision ID: a4c9e2d71b03
Revises: f736452b1d88
Create Date: 2026-10-11 06:30:00.000000

`gpu_price_samples`: Krater's own Vast price history (`krater.services.price_history`), one row per GPU model,
rental type and sample time, with rates in integer micro-dollars per hour.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c9e2d71b03"
down_revision: str | Sequence[str] | None = "f736452b1d88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "gpu_price_samples",
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gpu_name", sa.String(length=64), nullable=False),
        sa.Column("rental_type", sa.String(length=16), nullable=False),
        sa.Column("num_gpus", sa.Integer(), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("offer_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("min_microusd", sa.BigInteger(), nullable=True),
        sa.Column("p10_microusd", sa.BigInteger(), nullable=True),
        sa.Column("median_microusd", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint("rental_type IN ('bid', 'ondemand')", name=op.f("ck_gpu_price_samples_rental_type")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gpu_price_samples")),
    )
    op.create_index("ix_gpu_price_samples_sampled_at", "gpu_price_samples", ["sampled_at"], unique=False)
    op.create_index(
        "ix_gpu_price_samples_series",
        "gpu_price_samples",
        ["gpu_name", "rental_type", "num_gpus", "sampled_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_gpu_price_samples_series", table_name="gpu_price_samples")
    op.drop_index("ix_gpu_price_samples_sampled_at", table_name="gpu_price_samples")
    op.drop_table("gpu_price_samples")
