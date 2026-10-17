"""add spend reconciliations

Revision ID: b9a50b19751b
Revises: b81f0c6d2e57
Create Date: 2026-10-11 09:00:00.000000

Hourly reconciliation of Krater Compute's spend estimate with Vast's own charges: `spend_reconciliations` (one row
per project per check) and `compute_instances.charged_microusd` / `charged_at`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9a50b19751b"
down_revision: str | Sequence[str] | None = "b81f0c6d2e57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "spend_reconciliations",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instance_count", sa.Integer(), nullable=False),
        sa.Column("estimate_microusd", sa.BigInteger(), nullable=False),
        sa.Column("actual_microusd", sa.BigInteger(), nullable=False),
        sa.Column("alerted", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_spend_reconciliations_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_reconciliations")),
    )
    op.create_index(
        "ix_spend_reconciliations_project_id_checked_at",
        "spend_reconciliations",
        ["project_id", "checked_at"],
        unique=False,
    )
    op.add_column("compute_instances", sa.Column("charged_microusd", sa.BigInteger(), nullable=True))
    op.add_column("compute_instances", sa.Column("charged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("compute_instances", "charged_at")
    op.drop_column("compute_instances", "charged_microusd")
    op.drop_index("ix_spend_reconciliations_project_id_checked_at", table_name="spend_reconciliations")
    op.drop_table("spend_reconciliations")
