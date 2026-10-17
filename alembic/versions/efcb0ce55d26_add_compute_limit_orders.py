"""add compute limit orders

Revision ID: efcb0ce55d26
Revises: 2d64a8c637d2
Create Date: 2026-10-14 16:10:00.000000

`compute_jobs.on_outbid`: what a job does when it stays outbid past the policy's "move after" time. `move` (every
existing job) destroys the machine and starts again elsewhere; `hold`, which limit orders use, keeps the paused
machine and its disk, and moves only once a machine at or under the job's max is free.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "efcb0ce55d26"
down_revision: str | Sequence[str] | None = "2d64a8c637d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compute_jobs",
        sa.Column(
            "on_outbid",
            sa.Enum("move", "hold", name="compute_outbid", native_enum=False, create_constraint=True, length=64),
            server_default="move",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_compute_jobs_compute_outbid"), "compute_jobs", type_="check")
    op.drop_column("compute_jobs", "on_outbid")
