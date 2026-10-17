"""add runner self stop

Revision ID: 2d64a8c637d2
Revises: f5b2d8e1a936
Create Date: 2026-10-14 15:51:26.293588

A runner that can't reach Krater for a while stops its own machine (staging 2026-10-14: a box billed for four hours
while Krater was down). The policy number for that (180 minutes by default), and `watch_resumed_at` on instances, so
the scheduler's runner deadlines don't count Krater's own downtime against a runner.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d64a8c637d2"
down_revision: str | Sequence[str] | None = "f5b2d8e1a936"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("compute_instances", sa.Column("watch_resumed_at", sa.DateTime(timezone=True), nullable=True))
    # Seed the existing defaults row through a server default, then drop the default: the row is the source.
    op.add_column(
        "compute_settings", sa.Column("runner_self_stop_minutes", sa.Integer(), server_default="180", nullable=False)
    )
    op.alter_column("compute_settings", "runner_self_stop_minutes", server_default=None)
    op.add_column("project_compute_settings", sa.Column("runner_self_stop_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("project_compute_settings", "runner_self_stop_minutes")
    op.drop_column("compute_settings", "runner_self_stop_minutes")
    op.drop_column("compute_instances", "watch_resumed_at")
