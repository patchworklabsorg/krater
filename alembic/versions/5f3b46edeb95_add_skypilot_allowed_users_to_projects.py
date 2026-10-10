"""add skypilot allowed users to projects

Revision ID: 5f3b46edeb95
Revises: b7ba424bfaeb
Create Date: 2026-10-10 12:07:34.645919

`projects.skypilot_allowed_users` holds the `allowed_users` list the SkyPilot reconciler last sent to the
project's workspace. The reconciler compares it with the next list to find people it removed because
Weave no longer lists them as active members, and audits each removal once. It starts empty (`NULL`):
those workspaces hold the whole team, and the next reconcile fills it in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f3b46edeb95"
down_revision: str | Sequence[str] | None = "b7ba424bfaeb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("skypilot_allowed_users", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "skypilot_allowed_users")
