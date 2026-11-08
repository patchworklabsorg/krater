"""add ospool_grants and ospool_usage_days

Revision ID: e5b7d9f1a3c6
Revises: a1c4e7f2b9d3
Create Date: 2026-11-08 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b7d9f1a3c6"
down_revision: str | Sequence[str] | None = "a1c4e7f2b9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ospool_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ospool_project_name", sa.String(length=128), nullable=False),
        sa.Column("counted_from", sa.Date(), nullable=False),
        sa.Column("gpu_name", sa.String(length=64), nullable=False),
        sa.Column("rate_override_microusd", sa.BigInteger(), nullable=True),
        sa.Column("rate_note", sa.String(length=200), server_default="", nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_import_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ospool_project_name"),
        sa.UniqueConstraint("project_id"),
    )
    op.create_table(
        "ospool_usage_days",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("gpu_hours", sa.Numeric(14, 4), nullable=False),
        sa.Column("core_hours", sa.Numeric(14, 4), nullable=False),
        sa.Column("jobs", sa.Integer(), nullable=False),
        sa.Column("rate_microusd", sa.BigInteger(), nullable=True),
        sa.Column("rate_source", sa.String(length=16), nullable=True),
        sa.Column("value_cents", sa.BigInteger(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["ospool_grants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("grant_id", "day", name="uq_ospool_usage_days_grant_day"),
    )
    op.create_index(op.f("ix_ospool_usage_days_grant_id"), "ospool_usage_days", ["grant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ospool_usage_days_grant_id"), table_name="ospool_usage_days")
    op.drop_table("ospool_usage_days")
    op.drop_table("ospool_grants")
