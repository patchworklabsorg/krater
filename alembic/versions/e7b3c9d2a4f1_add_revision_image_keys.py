"""add revision image keys

Revision ID: e7b3c9d2a4f1
Revises: d4e9a1b7c2f6
Create Date: 2026-11-03 21:00:00.000000

`project_revisions.image_keys`: the images a completion's team uploaded for its final write-up, which the
Markdown shows inline (`/projects/<id>/images/<name>`). `screenshot_keys` now holds one image at most, the
primary image the gallery shows on its list and at the top of the page. A revision that had more keeps its
first as the primary and moves the rest to `image_keys`, so they stay hosted and can be put in the text.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b3c9d2a4f1"
down_revision: str | Sequence[str] | None = "d4e9a1b7c2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "project_revisions",
        sa.Column("image_keys", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
    )
    # Postgres arrays start at 1: keep the first screenshot as the primary image, move the rest.
    op.execute(
        "UPDATE project_revisions SET image_keys = screenshot_keys[2:], screenshot_keys = screenshot_keys[1:1] "
        "WHERE cardinality(screenshot_keys) > 1"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE project_revisions SET screenshot_keys = screenshot_keys || image_keys")
    op.drop_column("project_revisions", "image_keys")
