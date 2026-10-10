"""make audit_events.actor_id nullable

Revision ID: cdd8685b4aa7
Revises: 153093277378
Create Date: 2026-09-26 21:14:38.215192

The SkyPilot reconciler (`krater.services.skypilot_sync`) writes `budget_warning`, `budget_teardown`
and workspace-teardown audit events as a system actor with no human `User` behind it -- there's no one
to attribute a periodic reconcile run to. Every human-triggered action still always supplies an actor.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cdd8685b4aa7"
down_revision: str | Sequence[str] | None = "153093277378"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("audit_events", "actor_id", existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("audit_events", "actor_id", existing_type=sa.UUID(), nullable=False)
