"""add compute guards

Revision ID: f2a6c8e4b1d9
Revises: e5f8b3d1a7c4
Create Date: 2026-11-11 22:00:00.000000

What Krater needs before compute is turned on for members: the stop switch and organization-wide spending caps
(`compute_settings`), a per-project machine limit and a warning for a batch job whose GPU sits idle (both policy
fields, so a project can override them), the notice in a project's channel when a batch job ends, deleting a
finished project's files on a date fixed when it finishes, and what the worker last saw of the provider account's
credit and of storage use.

Every new column is nullable or has a server default. Jobs that ended before this are marked as told, so the first
pass doesn't post a notice for each of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a6c8e4b1d9"
down_revision: str | Sequence[str] | None = "e5f8b3d1a7c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("compute_settings", sa.Column("max_machines", sa.Integer(), nullable=False, server_default="4"))
    op.add_column(
        "compute_settings", sa.Column("job_idle_warn_minutes", sa.Integer(), nullable=False, server_default="30")
    )
    op.add_column(
        "compute_settings", sa.Column("org_daily_cap_cents", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "compute_settings", sa.Column("org_monthly_cap_cents", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("compute_settings", sa.Column("credit_alert_cents", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "compute_settings", sa.Column("finished_files_days", sa.Integer(), nullable=False, server_default="30")
    )
    op.add_column("compute_settings", sa.Column("paused_at", _TZ, nullable=True))
    op.add_column("compute_settings", sa.Column("paused_reason", sa.Text(), nullable=True))
    op.add_column(
        "compute_settings",
        sa.Column("paused_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column("compute_settings", sa.Column("halted_since", _TZ, nullable=True))
    op.add_column("compute_settings", sa.Column("halt_reason", sa.Text(), nullable=True))

    op.add_column("project_compute_settings", sa.Column("max_machines", sa.Integer(), nullable=True))
    op.add_column("project_compute_settings", sa.Column("job_idle_warn_minutes", sa.Integer(), nullable=True))

    op.add_column("projects", sa.Column("files_delete_on", _TZ, nullable=True))
    op.add_column("projects", sa.Column("files_deleted_at", _TZ, nullable=True))

    op.add_column("compute_jobs", sa.Column("end_notified_at", _TZ, nullable=True))
    op.execute("UPDATE compute_jobs SET end_notified_at = ended_at WHERE ended_at IS NOT NULL")
    op.add_column("compute_instances", sa.Column("idle_warned_at", _TZ, nullable=True))

    op.create_table(
        "provider_accounts",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("credit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", _TZ, nullable=False),
    )
    op.create_table(
        "storage_usage",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("objects", sa.Integer(), nullable=False),
        sa.Column("observed_at", _TZ, nullable=False),
    )
    op.create_index("ix_storage_usage_project_id", "storage_usage", ["project_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_storage_usage_project_id", table_name="storage_usage")
    op.drop_table("storage_usage")
    op.drop_table("provider_accounts")
    op.drop_column("compute_instances", "idle_warned_at")
    op.drop_column("compute_jobs", "end_notified_at")
    op.drop_column("projects", "files_deleted_at")
    op.drop_column("projects", "files_delete_on")
    op.drop_column("project_compute_settings", "job_idle_warn_minutes")
    op.drop_column("project_compute_settings", "max_machines")
    for name in (
        "halt_reason",
        "halted_since",
        "paused_by_id",
        "paused_reason",
        "paused_at",
        "finished_files_days",
        "credit_alert_cents",
        "org_monthly_cap_cents",
        "org_daily_cap_cents",
        "job_idle_warn_minutes",
        "max_machines",
    ):
        op.drop_column("compute_settings", name)
