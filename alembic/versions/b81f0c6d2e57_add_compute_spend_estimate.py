"""add compute spend estimate

Revision ID: b81f0c6d2e57
Revises: 23eb52c373d1
Create Date: 2026-10-11 08:00:00.000000

Krater Compute's live spend estimate: `compute_instances.accrued_microusd` / `accrued_until`, and a new
`spend_snapshots.source` value, `vast_estimate`. `source` is a non-native enum with a CHECK constraint, which
autogenerate doesn't compare, so the constraint is replaced by hand here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b81f0c6d2e57"
down_revision: str | Sequence[str] | None = "23eb52c373d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compute_instances", sa.Column("accrued_microusd", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("compute_instances", sa.Column("accrued_until", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_spend_snapshots_spend_source"), "spend_snapshots", type_="check")
    op.create_check_constraint(
        op.f("ck_spend_snapshots_spend_source"),
        "spend_snapshots",
        "source IN ('skypilot_cost_report', 'vast_estimate')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM spend_snapshots WHERE source = 'vast_estimate'")
    op.drop_constraint(op.f("ck_spend_snapshots_spend_source"), "spend_snapshots", type_="check")
    op.create_check_constraint(
        op.f("ck_spend_snapshots_spend_source"), "spend_snapshots", "source IN ('skypilot_cost_report')"
    )
    op.drop_column("compute_instances", "accrued_until")
    op.drop_column("compute_instances", "accrued_microusd")
