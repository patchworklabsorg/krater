"""add the open-source question's answer to project_revisions

Revision ID: b8e2f4a6c1d3
Revises: e7b3c9d2a4f1
Create Date: 2026-11-06 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e2f4a6c1d3"
down_revision: str | Sequence[str] | None = "e7b3c9d2a4f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_revisions", sa.Column("open_source_obstacles", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("project_revisions", "open_source_obstacles")
