"""add compute boxes

Revision ID: 4cd4032b89cb
Revises: d5e1a7b3c204
Create Date: 2026-10-11 12:00:00.000000

Interactive boxes: members' SSH public keys (`user_ssh_keys`), the stop/resume and SSH columns on
`compute_instances`, a `stopped` state for jobs and instances, and two policy numbers (box sync deadline and resume
timeout, 10 minutes each). The state columns are non-native enums with CHECK constraints, which autogenerate doesn't
compare, so those are replaced by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4cd4032b89cb"
down_revision: str | Sequence[str] | None = "d5e1a7b3c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_STATES = ("queued", "starting", "running", "paused", "succeeded", "failed", "cancelled")
_INSTANCE_STATES = ("creating", "provisioning", "running", "paused", "destroyed", "create_failed")
_INSTANCE_COLUMNS = (
    ("stop_requested_at", sa.DateTime(timezone=True)),
    ("stop_reason", sa.String(length=200)),
    ("last_synced_at", sa.DateTime(timezone=True)),
    ("stopped_at", sa.DateTime(timezone=True)),
    ("start_requested_at", sa.DateTime(timezone=True)),
    ("idle_since", sa.DateTime(timezone=True)),
    ("ssh_host", sa.String(length=255)),
    ("ssh_port", sa.Integer()),
)


def _states_check(table: str, name: str, column: str, states: Sequence[str]) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, f"{column} IN ({', '.join(repr(s) for s in states)})")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_ssh_keys",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_type", sa.String(length=40), nullable=False),
        sa.Column("key_base64", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=100), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_ssh_keys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_ssh_keys")),
        sa.UniqueConstraint("user_id", "fingerprint", name="user_fingerprint"),
    )
    op.create_index(op.f("ix_user_ssh_keys_user_id"), "user_ssh_keys", ["user_id"], unique=False)

    for name, type_ in _INSTANCE_COLUMNS:
        op.add_column("compute_instances", sa.Column(name, type_, nullable=True))

    for name in ("box_sync_deadline_minutes", "box_resume_timeout_minutes"):
        op.add_column("compute_settings", sa.Column(name, sa.Integer(), server_default="10", nullable=False))
        op.alter_column("compute_settings", name, server_default=None)
        op.add_column("project_compute_settings", sa.Column(name, sa.Integer(), nullable=True))

    _states_check("compute_jobs", "ck_compute_jobs_compute_job_state", "state", (*_JOB_STATES, "stopped"))
    _states_check(
        "compute_instances", "ck_compute_instances_compute_instance_state", "state", (*_INSTANCE_STATES, "stopped")
    )


def downgrade() -> None:
    """Downgrade schema."""
    _states_check("compute_instances", "ck_compute_instances_compute_instance_state", "state", _INSTANCE_STATES)
    _states_check("compute_jobs", "ck_compute_jobs_compute_job_state", "state", _JOB_STATES)
    for name in ("box_resume_timeout_minutes", "box_sync_deadline_minutes"):
        op.drop_column("project_compute_settings", name)
        op.drop_column("compute_settings", name)
    for name, _ in reversed(_INSTANCE_COLUMNS):
        op.drop_column("compute_instances", name)
    op.drop_index(op.f("ix_user_ssh_keys_user_id"), table_name="user_ssh_keys")
    op.drop_table("user_ssh_keys")
