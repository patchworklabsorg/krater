"""add job inputs and job-ending DMs

Revision ID: c4e1a8d2f6b7
Revises: a7c3e9f15b20
Create Date: 2026-11-12 03:00:00.000000

A job can copy files from its project's inputs (`projects/{id}/inputs/` in storage) onto its machine before its
command runs: the names it asked for, and their size for the data transfer estimate. An org setting to message a
job's submitter in Slack when it ends, and a record of each message sent so it goes once.

Every new column has a server default: jobs already queued take no inputs, and nobody is messaged until an admin
turns it on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e1a8d2f6b7"
down_revision: str | Sequence[str] | None = "a7c3e9f15b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compute_jobs",
        sa.Column("input_names", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column("compute_jobs", sa.Column("inputs_bytes", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("compute_settings", sa.Column("dm_job_endings", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "slack_direct_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("audit_event_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_slack_direct_messages_audit_event_id"), "slack_direct_messages", ["audit_event_id"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_slack_direct_messages_audit_event_id"), table_name="slack_direct_messages")
    op.drop_table("slack_direct_messages")
    op.drop_column("compute_settings", "dm_job_endings")
    op.drop_column("compute_jobs", "inputs_bytes")
    op.drop_column("compute_jobs", "input_names")
