"""add compute instance provider

Revision ID: a7c3e91f0b42
Revises: efcb0ce55d26
Create Date: 2026-10-14 16:35:00.000000

`compute_instances.provider`: which provider (`krater.providers`) the machine was rented from. Every machine so far
is Vast's. The provider's own id for it stays in `vast_instance_id` (renamed when a second provider arrives, see
docs/compute/providers.md), now unique per provider rather than across them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e91f0b42"
down_revision: str | Sequence[str] | None = "efcb0ce55d26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compute_instances", sa.Column("provider", sa.String(length=32), server_default="vast", nullable=False)
    )
    op.drop_constraint(op.f("uq_compute_instances_vast_instance_id"), "compute_instances", type_="unique")
    op.create_unique_constraint("provider_instance", "compute_instances", ["provider", "vast_instance_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("provider_instance", "compute_instances", type_="unique")
    op.create_unique_constraint(
        op.f("uq_compute_instances_vast_instance_id"), "compute_instances", ["vast_instance_id"]
    )
    op.drop_column("compute_instances", "provider")
