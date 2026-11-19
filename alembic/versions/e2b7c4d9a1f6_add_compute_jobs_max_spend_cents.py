"""add compute_jobs.max_spend_cents: a job's own spend cap (`--max-spend`)

Revision ID: e2b7c4d9a1f6
Revises: c5d1e8a3f7b4
Create Date: 2026-11-19 22:30:00
"""

import sqlalchemy as sa

from alembic import op

revision = "e2b7c4d9a1f6"
down_revision = "c5d1e8a3f7b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("compute_jobs", sa.Column("max_spend_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("compute_jobs", "max_spend_cents")
