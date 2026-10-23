"""add storage reservations and ops alerts

Revision ID: d4e9a1b7c2f6
Revises: c8f2a4d6e1b3
Create Date: 2026-10-22 18:00:00.000000

`storage_reservations`: the space each runner upload was promised until its URLs expire, so parallel uploads can't
overrun a project's storage quota between being signed and finishing. `ops_alerts`: the problems the worker tells
admins about in Slack, and when it last did. `worker_heartbeat`: one row, the worker's last heartbeat, for
`/healthz/worker`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e9a1b7c2f6"
down_revision: str | Sequence[str] | None = "c8f2a4d6e1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "storage_reservations",
        sa.Column("key", sa.String(length=1024), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_storage_reservations_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_storage_reservations")),
    )
    op.create_index(op.f("ix_storage_reservations_project_id"), "storage_reservations", ["project_id"], unique=False)
    op.create_table(
        "ops_alerts",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_ops_alerts")),
    )
    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_worker_heartbeat_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_heartbeat")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("worker_heartbeat")
    op.drop_table("ops_alerts")
    op.drop_index(op.f("ix_storage_reservations_project_id"), table_name="storage_reservations")
    op.drop_table("storage_reservations")
