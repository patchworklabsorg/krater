"""add compute_jobs.smoke_minutes: smoke tests (`krater submit --smoke`)

Revision ID: b7e3f1a9c2d5
Revises: a4d8e2b6c0f3
Create Date: 2026-11-13 12:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "b7e3f1a9c2d5"
down_revision = "a4d8e2b6c0f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("compute_jobs", sa.Column("smoke_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("compute_jobs", "smoke_minutes")
