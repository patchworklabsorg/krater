"""add compute jobs, instances and events

Revision ID: 23eb52c373d1
Revises: a4c9e2d71b03
Create Date: 2026-10-11 07:00:00.000000

Krater Compute's queue: `compute_jobs` (what members asked for, and where each job is), `compute_instances` (every
Vast instance rented for a job, labelled so a create whose response was lost can be found again) and
`compute_events` (an append-only log per job). Each enum column's `sa.Enum(..., create_constraint=True)` attaches
its own CHECK constraint, as in the initial schema migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "23eb52c373d1"
down_revision: str | Sequence[str] | None = "a4c9e2d71b03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "compute_jobs",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("submitter_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("job", "box", name="compute_job_kind", native_enum=False, create_constraint=True, length=64),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("image", sa.String(length=500), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("gpu_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("min_gpu_ram_gb", sa.Integer(), nullable=True),
        sa.Column("num_gpus", sa.Integer(), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("target_microusd", sa.BigInteger(), nullable=False),
        sa.Column("max_microusd", sa.BigInteger(), nullable=False),
        sa.Column("interruptible", sa.Boolean(), nullable=False),
        sa.Column("max_wait_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "on_max_wait",
            sa.Enum(
                "keep_waiting",
                "raise_bid",
                "on_demand",
                "cancel",
                name="compute_escalation",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("max_runtime_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "starting",
                "running",
                "paused",
                "succeeded",
                "failed",
                "cancelled",
                name="compute_job_state",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("hold_cents", sa.Integer(), nullable=False),
        sa.Column("spend_cents", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "num_gpus >= 1 AND disk_gb >= 1 AND max_runtime_minutes >= 1", name=op.f("ck_compute_jobs_shape")
        ),
        sa.CheckConstraint(
            "target_microusd > 0 AND target_microusd <= max_microusd", name=op.f("ck_compute_jobs_prices")
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_compute_jobs_project_id_projects")),
        sa.ForeignKeyConstraint(["submitter_id"], ["users.id"], name=op.f("fk_compute_jobs_submitter_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compute_jobs")),
    )
    op.create_index(op.f("ix_compute_jobs_project_id"), "compute_jobs", ["project_id"], unique=False)
    op.create_index("ix_compute_jobs_state_queued_at", "compute_jobs", ["state", "queued_at"], unique=False)
    op.create_table(
        "compute_instances",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("vast_instance_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "creating",
                "provisioning",
                "running",
                "paused",
                "destroyed",
                "create_failed",
                name="compute_instance_state",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("offer_id", sa.BigInteger(), nullable=False),
        sa.Column("machine_id", sa.BigInteger(), nullable=True),
        sa.Column("gpu_name", sa.String(length=64), nullable=False),
        sa.Column("interruptible", sa.Boolean(), nullable=False),
        sa.Column("gpu_microusd", sa.BigInteger(), nullable=False),
        sa.Column("disk_microusd", sa.BigInteger(), nullable=False),
        sa.Column("last_vast_status", sa.String(length=64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("running_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("destroy_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("destroy_reason", sa.String(length=200), nullable=True),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["compute_jobs.id"], name=op.f("fk_compute_instances_job_id_compute_jobs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compute_instances")),
        sa.UniqueConstraint("job_id", "attempt", name="job_attempt"),
        sa.UniqueConstraint("label", name=op.f("uq_compute_instances_label")),
        sa.UniqueConstraint("vast_instance_id", name=op.f("uq_compute_instances_vast_instance_id")),
    )
    op.create_index(op.f("ix_compute_instances_job_id"), "compute_instances", ["job_id"], unique=False)
    op.create_table(
        "compute_events",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instance_id"], ["compute_instances.id"], name=op.f("fk_compute_events_instance_id_compute_instances")
        ),
        sa.ForeignKeyConstraint(["job_id"], ["compute_jobs.id"], name=op.f("fk_compute_events_job_id_compute_jobs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compute_events")),
    )
    op.create_index("ix_compute_events_job_id_at", "compute_events", ["job_id", "at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_compute_events_job_id_at", table_name="compute_events")
    op.drop_table("compute_events")
    op.drop_index(op.f("ix_compute_instances_job_id"), table_name="compute_instances")
    op.drop_table("compute_instances")
    op.drop_index("ix_compute_jobs_state_queued_at", table_name="compute_jobs")
    op.drop_index(op.f("ix_compute_jobs_project_id"), table_name="compute_jobs")
    op.drop_table("compute_jobs")
