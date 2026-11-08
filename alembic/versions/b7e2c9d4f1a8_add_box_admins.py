"""add box_admins

Revision ID: b7e2c9d4f1a8
Revises: e5b7d9f1a3c6
Create Date: 2026-11-08 07:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2c9d4f1a8"
down_revision: str | Sequence[str] | None = "e5b7d9f1a3c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "box_admins",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("box_admins")
