"""add admin_box_access: an admin's time-limited SSH access to a running box

Revision ID: e8b4c2f7a1d3
Revises: d7a3e9f1b4c6
Create Date: 2026-11-07 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b4c2f7a1d3"
down_revision: str | Sequence[str] | None = "d7a3e9f1b4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_box_access",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("compute_jobs.id"), nullable=False),
        sa.Column("admin_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_box_access_job_id", "admin_box_access", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_box_access_job_id", table_name="admin_box_access")
    op.drop_table("admin_box_access")
