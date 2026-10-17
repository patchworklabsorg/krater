"""add the application form's answers to project_revisions

Revision ID: f5b2d8e1a936
Revises: e3a9c6b2d714
Create Date: 2026-10-12 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5b2d8e1a936"
down_revision: str | Sequence[str] | None = "e3a9c6b2d714"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_revisions", sa.Column("summary", sa.String(length=300), server_default="", nullable=False))
    op.add_column("project_revisions", sa.Column("experience", sa.Text(), server_default="", nullable=False))
    op.add_column(
        "project_revisions", sa.Column("affiliation", sa.String(length=16), server_default="none", nullable=False)
    )
    op.add_column("project_revisions", sa.Column("affiliation_detail", sa.Text(), server_default="", nullable=False))
    op.add_column("project_revisions", sa.Column("compute_use", sa.Text(), server_default="", nullable=False))
    op.add_column("project_revisions", sa.Column("full_budget_cents", sa.Integer(), nullable=True))
    op.add_column(
        "project_revisions",
        sa.Column("full_budget_estimate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    for column in (
        "full_budget_estimate",
        "full_budget_cents",
        "compute_use",
        "affiliation_detail",
        "affiliation",
        "experience",
        "summary",
    ):
        op.drop_column("project_revisions", column)
