"""add stash archive problem

Revision ID: a1c4e2f7b9d3
Revises: ebee8a6d6d16
Create Date: 2026-11-09 05:00:00.000000

Stash's `problem` on a `partial` or `failed` archive (its `docs/api.md`): the error code of the failed capture
that matters most to the submitter, the hint for a person, and the message for whoever debugs. The sender copies
them on each status refresh, so the project page can say what to fix and who can fix it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e2f7b9d3"
down_revision: str | Sequence[str] | None = "ebee8a6d6d16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("stash_archives", sa.Column("problem_code", sa.String(length=32), nullable=True))
    op.add_column("stash_archives", sa.Column("problem_hint", sa.Text(), nullable=True))
    op.add_column("stash_archives", sa.Column("problem_message", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("stash_archives", "problem_message")
    op.drop_column("stash_archives", "problem_hint")
    op.drop_column("stash_archives", "problem_code")
