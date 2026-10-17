"""merge PR #1's member-data change (a7c1e5d9f2b4) into Krater Compute's history

PR #1 drops the stored member data straight after its last migration; Krater Compute's migrations branch off that
same point. This joins the two heads; it changes nothing itself.

Revision ID: b2d4f6a8c0e1
Revises: c4e8a2f7b913, a7c1e5d9f2b4
Create Date: 2026-10-12 03:30:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b2d4f6a8c0e1"
down_revision: str | Sequence[str] | None = ("c4e8a2f7b913", "a7c1e5d9f2b4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
