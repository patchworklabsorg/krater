"""add gpu prices and revision budget estimate

Revision ID: 29fc5db971e5
Revises: 9ed6e4593cff
Create Date: 2026-09-27 03:11:25.600703

`gpu_prices` holds the latest aggregated Vast pricing (`krater.services.pricing.refresh_prices`
replaces it wholesale on every refresh -- no history table). `project_revisions.budget_estimate` is the
server-recomputed breakdown from the budget estimator, set only when the submitter used it. See
docs/dev/pricing.md.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "29fc5db971e5"
down_revision: str | Sequence[str] | None = "9ed6e4593cff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "gpu_prices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("accelerator_name", sa.String(length=64), nullable=False),
        sa.Column("accelerator_count", sa.Integer(), nullable=False),
        sa.Column("vram_gib", sa.Float(), nullable=True),
        sa.Column("vcpus_typical", sa.Float(), nullable=True),
        sa.Column("memory_gib_typical", sa.Float(), nullable=True),
        sa.Column("on_demand_min_cents", sa.Integer(), nullable=False),
        sa.Column("on_demand_median_cents", sa.Integer(), nullable=False),
        sa.Column("spot_min_cents", sa.Integer(), nullable=True),
        sa.Column("offer_count", sa.Integer(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gpu_prices")),
        sa.UniqueConstraint("accelerator_name", "accelerator_count", name="uq_gpu_prices_accelerator_name"),
    )
    op.create_index(op.f("ix_gpu_prices_accelerator_name"), "gpu_prices", ["accelerator_name"], unique=False)
    op.add_column(
        "project_revisions",
        sa.Column("budget_estimate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("project_revisions", "budget_estimate")
    op.drop_index(op.f("ix_gpu_prices_accelerator_name"), table_name="gpu_prices")
    op.drop_table("gpu_prices")
