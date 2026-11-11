"""add approved budget

Revision ID: c3f8a1d5e7b2
Revises: a1c4e2f7b9d3
Create Date: 2026-11-10 03:00:00.000000

An approver may grant less than a proposal or amendment asks for. `reviews.budget_cents` is the amount one approving
reviewer (or the admin override) chose; `project_revisions.budget_approved_cents` is what the approval granted, set
when the revision is approved. Both null mean the full request, as on every row from before this.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f8a1d5e7b2"
down_revision: str | Sequence[str] | None = "a1c4e2f7b9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("reviews", sa.Column("budget_cents", sa.Integer(), nullable=True))
    op.add_column("project_revisions", sa.Column("budget_approved_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("project_revisions", "budget_approved_cents")
    op.drop_column("reviews", "budget_cents")
