"""drop skypilot columns

Revision ID: f3b8d4e60a12
Revises: e7a2c9d1f305
Create Date: 2026-10-11 14:00:00.000000

SkyPilot is gone (Krater Compute replaces it), so `projects.skypilot_workspace` and `skypilot_allowed_users` go too.
The downgrade brings the columns back empty: the workspaces they named no longer exist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b8d4e60a12"
down_revision: str | Sequence[str] | None = "e7a2c9d1f305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("projects", "skypilot_allowed_users")
    op.drop_column("projects", "skypilot_workspace")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("projects", sa.Column("skypilot_workspace", sa.String(length=255), nullable=True))
    op.add_column(
        "projects", sa.Column("skypilot_allowed_users", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
