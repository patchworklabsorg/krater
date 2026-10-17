"""add compute run seconds

Revision ID: e7a2c9d1f305
Revises: 4cd4032b89cb
Create Date: 2026-10-11 13:00:00.000000

`compute_instances.run_seconds`: time actually spent running in the current run, which max runtime limits (paused and
stopped time don't count).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a2c9d1f305"
down_revision: str | Sequence[str] | None = "4cd4032b89cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("compute_instances", sa.Column("run_seconds", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("compute_instances", "run_seconds")
