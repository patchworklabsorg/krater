"""add slack integration fields

Revision ID: 9ed6e4593cff
Revises: cdd8685b4aa7
Create Date: 2026-09-26 22:41:17.333902

Adds what `krater.services.slack_notify` needs: a project's channel-archived flag (kept separate from
`slack_channel_id`, which stays set after archiving so the project page can still link to it), the
review message a revision's decision later updates in place, and `slack_notifications` -- tracking which
`budget_warning`/`budget_teardown` audit events the periodic Slack reconcile has already posted, so it
never posts one twice.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9ed6e4593cff"
down_revision: str | Sequence[str] | None = "cdd8685b4aa7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects", sa.Column("slack_channel_archived", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("project_revisions", sa.Column("slack_message_channel_id", sa.String(length=64), nullable=True))
    op.add_column("project_revisions", sa.Column("slack_message_ts", sa.String(length=32), nullable=True))

    op.create_table(
        "slack_notifications",
        sa.Column("audit_event_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["audit_event_id"], ["audit_events.id"], name=op.f("fk_slack_notifications_audit_event_id_audit_events")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slack_notifications")),
    )
    op.create_index(
        op.f("ix_slack_notifications_audit_event_id"), "slack_notifications", ["audit_event_id"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_slack_notifications_audit_event_id"), table_name="slack_notifications")
    op.drop_table("slack_notifications")

    op.drop_column("project_revisions", "slack_message_ts")
    op.drop_column("project_revisions", "slack_message_channel_id")
    op.drop_column("projects", "slack_channel_archived")
