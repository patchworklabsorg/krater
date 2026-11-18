"""add project_updates: progress updates and top-up requests

A project's team posts progress updates; one with an amount is a top-up request, which opens an amendment
(`revision_id`). Only adds a table.

Revision ID: a7c4e2f9b1d3
Revises: d5a8c1f3e7b9
Create Date: 2026-11-18 17:00:00
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a7c4e2f9b1d3"
down_revision = "d5a8c1f3e7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_updates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("topup_cents", sa.Integer(), nullable=True),
        sa.Column("topup_reason", sa.Text(), nullable=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ceiling_cents", sa.Integer(), nullable=False),
        sa.Column("spent_cents", sa.Integer(), nullable=False),
        sa.Column("jobs_since", sa.Integer(), server_default="0", nullable=False),
        sa.Column("slack_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slack_message_ts", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["project_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id"),
    )
    op.create_index("ix_project_updates_project_id_created_at", "project_updates", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_project_updates_project_id_created_at", table_name="project_updates")
    op.drop_table("project_updates")
