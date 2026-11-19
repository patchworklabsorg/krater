"""add image_facts, compute_jobs.min_cuda_major and compute_jobs.image_bytes: the checks on a job's image

Revision ID: a8d3f1c6e2b9
Revises: e2b7c4d9a1f6
Create Date: 2026-11-19 23:30:00
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a8d3f1c6e2b9"
down_revision = "e2b7c4d9a1f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_facts",
        sa.Column("digest", sa.String(length=80), primary_key=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("compute_jobs", sa.Column("min_cuda_major", sa.SmallInteger(), nullable=True))
    op.add_column("compute_jobs", sa.Column("image_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("compute_jobs", "image_bytes")
    op.drop_column("compute_jobs", "min_cuda_major")
    op.drop_table("image_facts")
