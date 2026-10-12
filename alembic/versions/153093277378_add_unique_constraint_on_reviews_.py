"""add unique constraint on reviews revision_id reviewer_id

Revision ID: 153093277378
Revises: ddcdc402852d
Create Date: 2026-09-26 19:37:31.411192

`record_review`'s "already reviewed" check is a SELECT followed by an INSERT, so two concurrent
approvals from the same reviewer (a double-click, or a retried Slack action) could both pass the
check before either has inserted its row. This constraint is what actually prevents the duplicate;
`record_review` turns the resulting `IntegrityError` into a friendly `InvalidState`.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "153093277378"
down_revision: str | Sequence[str] | None = "ddcdc402852d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint("uq_reviews_revision_id_reviewer_id", "reviews", ["revision_id", "reviewer_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_reviews_revision_id_reviewer_id", "reviews", type_="unique")
