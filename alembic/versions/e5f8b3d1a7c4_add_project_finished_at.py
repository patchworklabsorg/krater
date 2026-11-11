"""add project finished_at

Revision ID: e5f8b3d1a7c4
Revises: d4e7a2c9f1b6
Create Date: 2026-11-11 18:00:00.000000

When a project became `completed` or `withdrawn`. Its Slack channels are archived a week after. Null for a project
that isn't finished, and for every one that finished before this: those are archived as before, at once.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f8b3d1a7c4"
down_revision: str | Sequence[str] | None = "d4e7a2c9f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("projects", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "finished_at")
