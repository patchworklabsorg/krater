"""add compute_jobs.checkpoint_upload_minutes: a job's own checkpoint upload interval (`--checkpoint-upload`)

Revision ID: c5d1e8a3f7b4
Revises: a7c4e2f9b1d3
Create Date: 2026-11-19 01:30:00
"""

import sqlalchemy as sa

from alembic import op

revision = "c5d1e8a3f7b4"
down_revision = "a7c4e2f9b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("compute_jobs", sa.Column("checkpoint_upload_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("compute_jobs", "checkpoint_upload_minutes")
