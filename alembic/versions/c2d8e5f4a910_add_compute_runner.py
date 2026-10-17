"""add compute runner

Revision ID: c2d8e5f4a910
Revises: b9a50b19751b
Create Date: 2026-10-11 10:00:00.000000

The runner on each rented machine: its token hash, starts and heartbeats on `compute_instances`, and two new policy
numbers, the runner start deadline (10 minutes) and the heartbeat timeout (30 minutes), as global defaults and
per-project overrides.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d8e5f4a910"
down_revision: str | Sequence[str] | None = "b9a50b19751b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("compute_instances", sa.Column("entered_running_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("compute_instances", sa.Column("runner_token_hash", sa.String(length=64), nullable=True))
    op.add_column("compute_instances", sa.Column("runner_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("compute_instances", sa.Column("runner_starts", sa.Integer(), server_default="0", nullable=False))
    op.add_column(
        "compute_instances", sa.Column("runner_starts_needed", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column("compute_instances", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "compute_instances",
        sa.Column("last_heartbeat", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_compute_instances_runner_token_hash"), "compute_instances", ["runner_token_hash"]
    )

    # Seed the existing defaults row through a server default, then drop the default: the row is the source.
    for name, default in (("runner_start_minutes", "10"), ("heartbeat_timeout_minutes", "30")):
        op.add_column("compute_settings", sa.Column(name, sa.Integer(), server_default=default, nullable=False))
        op.alter_column("compute_settings", name, server_default=None)
        op.add_column("project_compute_settings", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for name in ("heartbeat_timeout_minutes", "runner_start_minutes"):
        op.drop_column("project_compute_settings", name)
        op.drop_column("compute_settings", name)
    op.drop_constraint(op.f("uq_compute_instances_runner_token_hash"), "compute_instances", type_="unique")
    for name in (
        "last_heartbeat",
        "last_heartbeat_at",
        "runner_starts_needed",
        "runner_starts",
        "runner_started_at",
        "runner_token_hash",
        "entered_running_at",
    ):
        op.drop_column("compute_instances", name)
