"""add compute_settings.cpu_core_hour_microusd: Krater's price for a CPU core-hour

Revision ID: d3f8b1c5e7a2
Revises: a8d3f1c6e2b9
Create Date: 2026-11-25 22:00:00.000000

An org setting an admin sets on /admin/compute: what one CPU core costs per hour, in micro-dollars. The application
form prices a CPU-only estimate with it (cores x hours x this). The server default 0 means no price is set, and teams
give their own as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f8b1c5e7a2"
down_revision: str | Sequence[str] | None = "a8d3f1c6e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compute_settings", sa.Column("cpu_core_hour_microusd", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("compute_settings", "cpu_core_hour_microusd")
