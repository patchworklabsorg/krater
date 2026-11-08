"""add project_revisions.training_data/criteria_read_at, projects.funding_hold and reviews.funding_hold

Revision ID: a4e7c1f9d2b3
Revises: f1c9d3a5b7e2
Create Date: 2026-11-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4e7c1f9d2b3"
down_revision: str | Sequence[str] | None = "f1c9d3a5b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _funding_hold(constraint: str) -> sa.Enum:
    return sa.Enum(
        "awaiting_funding",
        "awaiting_third_party_review",
        name=constraint,
        native_enum=False,
        create_constraint=True,
        length=64,
    )


def upgrade() -> None:
    op.add_column("project_revisions", sa.Column("training_data", sa.Text(), server_default="", nullable=False))
    op.add_column("project_revisions", sa.Column("criteria_read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("funding_hold", _funding_hold("funding_hold"), nullable=True))
    op.add_column("reviews", sa.Column("funding_hold", _funding_hold("funding_hold"), nullable=True))


def downgrade() -> None:
    op.drop_column("reviews", "funding_hold")
    op.drop_column("projects", "funding_hold")
    op.drop_column("project_revisions", "criteria_read_at")
    op.drop_column("project_revisions", "training_data")
