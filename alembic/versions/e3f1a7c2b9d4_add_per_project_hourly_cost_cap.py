"""add per-project hourly cost cap

Revision ID: e3f1a7c2b9d4
Revises: b7ba424bfaeb
Create Date: 2026-10-10 18:40:00.000000

`projects.max_hourly_cost_cents`: an admin-set hourly price cap for this project's SkyPilot launches.
Nullable, and null for every existing project, which keeps using the global default
(`KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS`).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3f1a7c2b9d4"
down_revision: str | Sequence[str] | None = "b7ba424bfaeb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("projects", sa.Column("max_hourly_cost_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "max_hourly_cost_cents")
