"""cancel queued skypilot_reconcile tasks (the task no longer exists)

SkyPilot was removed (f3b8d4e60a12), and with it the `skypilot_reconcile` task. A worker that finds a queued job
for a task it doesn't know fails it on every start, so mark any left in procrastinate's queue as cancelled. Rows
are kept (procrastinate_periodic_defers references them). Does nothing where procrastinate's tables don't exist yet.

Revision ID: b6d3f9a1c2e7
Revises: a90b9c697fd0
Create Date: 2026-10-11 16:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d3f9a1c2e7"
down_revision: str | Sequence[str] | None = "a90b9c697fd0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('procrastinate_jobs') IS NOT NULL THEN
            UPDATE procrastinate_jobs SET status = 'cancelled'
            WHERE task_name = 'skypilot_reconcile' AND status = 'todo';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    pass  # nothing to restore: the task is gone
