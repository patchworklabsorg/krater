"""value CPU-only core-hours on partner grant days

Revision ID: e6a2c9d4f1b8
Revises: d3f8b1c5e7a2
Create Date: 2026-11-25 23:30:00.000000

Each usage day also keeps the core-hours of jobs that used no GPU, the CPU price per core-hour that values them, and
their value. All three are nullable: a day imported before this has no CPU-only figure yet (`NULL`), and the next import
reads the grant's whole history again to fill it in.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6a2c9d4f1b8"
down_revision: str | Sequence[str] | None = "d3f8b1c5e7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("ospool_usage_days", sa.Column("cpu_core_hours", sa.Numeric(14, 4), nullable=True))
    op.add_column("ospool_usage_days", sa.Column("core_rate_microusd", sa.BigInteger(), nullable=True))
    op.add_column("ospool_usage_days", sa.Column("core_value_cents", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("ospool_usage_days", "core_value_cents")
    op.drop_column("ospool_usage_days", "core_rate_microusd")
    op.drop_column("ospool_usage_days", "cpu_core_hours")
