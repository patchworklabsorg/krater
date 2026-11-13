"""add live_quotes and live_price_usage: live price checks for a job's shape

Revision ID: f2b6d9c4e8a1
Revises: e8a4c1f7b3d9
Create Date: 2026-11-13 03:30:00
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f2b6d9c4e8a1"
down_revision = "e8a4c1f7b3d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_quotes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("shape", postgresql.JSONB(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cheapest_microusd", sa.BigInteger(), nullable=True),
        sa.Column("offers", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("error", sa.String(300), nullable=True),
    )
    op.create_table(
        "live_price_usage",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("purpose", sa.String(32), primary_key=True),
        sa.Column("searches", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("live_price_usage")
    op.drop_table("live_quotes")
