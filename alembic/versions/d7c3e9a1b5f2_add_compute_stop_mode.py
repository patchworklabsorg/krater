"""add the stop switch's mode: a regular stop saves work first, an emergency stop doesn't

Revision ID: d7c3e9a1b5f2
Revises: c4e1a8d2f6b7
Create Date: 2026-11-12 15:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "d7c3e9a1b5f2"
down_revision = "c4e1a8d2f6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("compute_settings", sa.Column("paused_mode", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("compute_settings", "paused_mode")
