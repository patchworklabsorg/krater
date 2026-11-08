"""add compute_instances.image_digest/image_pinned and image_inspections

Revision ID: f1c9d3a5b7e2
Revises: e8b4c2f7a1d3
Create Date: 2026-11-07 06:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c9d3a5b7e2"
down_revision: str | Sequence[str] | None = "e8b4c2f7a1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("compute_instances", sa.Column("image_digest", sa.String(80), nullable=True))
    op.add_column(
        "compute_instances", sa.Column("image_pinned", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.create_table(
        "image_inspections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("image", sa.String(500), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("inspected_by_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_image_inspections_digest", "image_inspections", ["digest"])


def downgrade() -> None:
    op.drop_index("ix_image_inspections_digest", table_name="image_inspections")
    op.drop_table("image_inspections")
    op.drop_column("compute_instances", "image_pinned")
    op.drop_column("compute_instances", "image_digest")
