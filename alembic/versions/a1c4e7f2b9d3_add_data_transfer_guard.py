"""add data transfer guard

Revision ID: a1c4e7f2b9d3
Revises: e7b3c9d2a4f1
Create Date: 2026-11-04 12:00:00.000000

Vast bills data into and out of a machine per GB, at the host's own price, on top of the hourly cost Krater
compares offers by. A member renting directly on Vast spent half their budget on it. Two policy numbers guard it:
`transfer_cap_microusd_per_gb` (Krater rents no host above it, each way; default $20 per TB) and
`transfer_warn_percent` (members are warned when a job's transfer could cost, or has cost, this share of the
budget; default 10). Jobs carry the member's own estimate of the data in and out (`data_in_gb`, `data_out_gb`);
instances record the prices they were rented at and what Vast charged for transfer.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e7f2b9d3"
down_revision: str | Sequence[str] | None = "e7b3c9d2a4f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Seed the existing defaults row through a server default, then drop the default: the row is the source.
    for name, default in (("transfer_cap_microusd_per_gb", "20000"), ("transfer_warn_percent", "10")):
        op.add_column("compute_settings", sa.Column(name, sa.Integer(), server_default=default, nullable=False))
        op.alter_column("compute_settings", name, server_default=None)
        op.add_column("project_compute_settings", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column("compute_jobs", sa.Column("data_in_gb", sa.Integer(), nullable=True))
    op.add_column("compute_jobs", sa.Column("data_out_gb", sa.Integer(), nullable=True))
    op.add_column("compute_instances", sa.Column("transfer_in_microusd_per_gb", sa.BigInteger(), nullable=True))
    op.add_column("compute_instances", sa.Column("transfer_out_microusd_per_gb", sa.BigInteger(), nullable=True))
    op.add_column("compute_instances", sa.Column("transfer_charged_microusd", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("compute_instances", "transfer_charged_microusd")
    op.drop_column("compute_instances", "transfer_out_microusd_per_gb")
    op.drop_column("compute_instances", "transfer_in_microusd_per_gb")
    op.drop_column("compute_jobs", "data_out_gb")
    op.drop_column("compute_jobs", "data_in_gb")
    for name in ("transfer_warn_percent", "transfer_cap_microusd_per_gb"):
        op.drop_column("project_compute_settings", name)
        op.drop_column("compute_settings", name)
