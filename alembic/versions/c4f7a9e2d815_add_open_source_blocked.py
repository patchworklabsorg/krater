"""turn the open-source question into a yes/no with the answer asked only on yes

Revision ID: c4f7a9e2d815
Revises: b8e2f4a6c1d3
Create Date: 2026-11-06 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f7a9e2d815"
down_revision: str | Sequence[str] | None = "b8e2f4a6c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_revisions", sa.Column("open_source_blocked", sa.Boolean(), server_default="false", nullable=False)
    )
    # The question was free text for a few hours (b8e2f4a6c1d3). Any answer given then counts as yes, so reviewers
    # still see it; a team that wrote "nothing" can untick the box.
    op.execute("UPDATE project_revisions SET open_source_blocked = true WHERE btrim(open_source_obstacles) <> ''")


def downgrade() -> None:
    op.drop_column("project_revisions", "open_source_blocked")
