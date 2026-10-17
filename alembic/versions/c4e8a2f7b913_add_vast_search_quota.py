"""add vast_search_quota (the worker's latest view of Vast's daily search quota, for admins)

Revision ID: c4e8a2f7b913
Revises: b6d3f9a1c2e7
Create Date: 2026-10-11 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a2f7b913"
down_revision: str | Sequence[str] | None = "b6d3f9a1c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vast_search_quota",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("limit_rows", sa.Integer(), nullable=False),
        sa.Column("remaining_rows", sa.Integer(), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("low_alerted_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exhausted_alerted_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_vast_search_quota_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vast_search_quota")),
    )


def downgrade() -> None:
    op.drop_table("vast_search_quota")
