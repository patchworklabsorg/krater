"""add review channel

Revision ID: d4e7a2c9f1b6
Revises: c3f8a1d5e7b2
Create Date: 2026-11-11 06:00:00.000000

Each project gets a second, reviewers-only Slack channel for the review message and reviewers' own talk. Created on the
first submission after this release; projects from before have none (null), and their review messages stay in the
team channel. `project_revisions.slack_team_message_ts` is the notice the team channel gets instead, which the decision
then updates.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e7a2c9f1b6"
down_revision: str | Sequence[str] | None = "c3f8a1d5e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("projects", sa.Column("slack_review_channel_id", sa.String(length=64), nullable=True))
    op.add_column(
        "projects",
        sa.Column("slack_review_channel_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("project_revisions", sa.Column("slack_team_message_ts", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("project_revisions", "slack_team_message_ts")
    op.drop_column("projects", "slack_review_channel_archived")
    op.drop_column("projects", "slack_review_channel_id")
