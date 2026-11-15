"""add ops_alert_failures: each failure behind an admin alert, for the admin compute page

Revision ID: c4f9a2e7d1b8
Revises: b7e3f1a9c2d5
Create Date: 2026-11-15 12:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "c4f9a2e7d1b8"
down_revision = "b7e3f1a9c2d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_alert_failures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_alert_failures")),
    )
    op.create_index("ix_ops_alert_failures_key_at", "ops_alert_failures", ["key", "at"])
    op.create_index("ix_ops_alert_failures_at", "ops_alert_failures", ["at"])


def downgrade() -> None:
    op.drop_index("ix_ops_alert_failures_at", table_name="ops_alert_failures")
    op.drop_index("ix_ops_alert_failures_key_at", table_name="ops_alert_failures")
    op.drop_table("ops_alert_failures")
