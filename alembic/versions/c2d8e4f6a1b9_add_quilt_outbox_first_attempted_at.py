"""add quilt_outbox.first_attempted_at

Revision ID: c2d8e4f6a1b9
Revises: a4e7c1f9d2b3
Create Date: 2026-11-07 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d8e4f6a1b9"
down_revision: str | Sequence[str] | None = "a4e7c1f9d2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: rows already tried get it on their next attempt.
    op.add_column("quilt_outbox", sa.Column("first_attempted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("quilt_outbox", "first_attempted_at")
