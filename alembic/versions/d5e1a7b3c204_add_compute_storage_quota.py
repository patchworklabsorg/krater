"""add compute storage quota

Revision ID: d5e1a7b3c204
Revises: c2d8e5f4a910
Create Date: 2026-10-11 11:00:00.000000

`storage_quota_gb`: how much a project's machines may keep in Krater's storage (checkpoints and outputs), as a global
default (50 GB) and a per-project override. Krater refuses upload URLs past it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e1a7b3c204"
down_revision: str | Sequence[str] | None = "c2d8e5f4a910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("compute_settings", sa.Column("storage_quota_gb", sa.Integer(), server_default="50", nullable=False))
    op.alter_column("compute_settings", "storage_quota_gb", server_default=None)
    op.add_column("project_compute_settings", sa.Column("storage_quota_gb", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("project_compute_settings", "storage_quota_gb")
    op.drop_column("compute_settings", "storage_quota_gb")
