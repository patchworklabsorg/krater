"""add edge_ping: when alastor's every-minute ping last reached the portal

Revision ID: e8b3c6d2f4a1
Revises: c4f9a2e7d1b8
Create Date: 2026-11-15 19:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "e8b3c6d2f4a1"
down_revision = "c4f9a2e7d1b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edge_ping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_edge_ping_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edge_ping")),
    )


def downgrade() -> None:
    op.drop_table("edge_ping")
