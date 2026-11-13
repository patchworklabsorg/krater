"""add live_prices: the job form's "Cheapest now" column

Revision ID: a4d8e2b6c0f3
Revises: f2b6d9c4e8a1
Create Date: 2026-11-13 05:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "a4d8e2b6c0f3"
down_revision = "f2b6d9c4e8a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_prices",
        sa.Column("gpu_name", sa.String(64), primary_key=True),
        sa.Column("rental_type", sa.String(16), primary_key=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cheapest_microusd", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("live_prices")
