"""add reviewer_groups snapshot to reviews

Revision ID: ddcdc402852d
Revises: 67a1656dcb6d
Create Date: 2026-09-26 19:19:22.841148

Snapshots the reviewer's Weave groups onto the `Review` row at the moment they review, so
`ApprovalPolicy.required_group` can be checked against the groups the reviewer actually held at
review time rather than their current (possibly since-changed) groups.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ddcdc402852d"
down_revision: str | Sequence[str] | None = "67a1656dcb6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "reviews",
        sa.Column("reviewer_groups", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("reviews", "reviewer_groups")
