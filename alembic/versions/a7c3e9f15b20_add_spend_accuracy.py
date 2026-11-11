"""add spend accuracy

Revision ID: a7c3e9f15b20
Revises: f2a6c8e4b1d9
Create Date: 2026-11-12 01:00:00.000000

Data transfer and the image pull in Krater's live spend estimate: each machine's network speeds, image size and the
bytes its runner reports, with the estimate split by item next to Vast's itemized charges. An optional credit
reserve before renting. And data for later price work: image sizes by digest, how long jobs wait for a machine at
their price, hosts' prices at each hourly sample, and each machine's market price and outbids.

Every new column is nullable or has a server default. Jobs already queued start their wall-clock wait from
`queued_at`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f15b20"
down_revision: str | Sequence[str] | None = "f2a6c8e4b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)


def _zero(name: str) -> sa.Column:
    return sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compute_settings", sa.Column("credit_reserve_hours", sa.Integer(), nullable=False, server_default="0")
    )

    op.add_column("compute_jobs", sa.Column("wait_started_at", _TZ, nullable=True))
    op.execute("UPDATE compute_jobs SET wait_started_at = queued_at WHERE wait_started_at IS NULL")

    op.add_column("compute_instances", sa.Column("link_down_mbps", sa.Integer(), nullable=True))
    op.add_column("compute_instances", sa.Column("link_up_mbps", sa.Integer(), nullable=True))
    op.add_column("compute_instances", sa.Column("image_bytes", sa.BigInteger(), nullable=True))
    for name in ("net_in_bytes", "net_out_bytes", "net_priced_in_bytes", "net_priced_out_bytes"):
        op.add_column("compute_instances", _zero(name))
    op.add_column("compute_instances", sa.Column("net_reported_at", _TZ, nullable=True))
    for name in (
        "est_gpu_microusd",
        "est_disk_microusd",
        "est_image_microusd",
        "est_transfer_in_microusd",
        "est_transfer_out_microusd",
    ):
        op.add_column("compute_instances", _zero(name))
    op.add_column("compute_instances", sa.Column("charged_items", postgresql.JSONB(), nullable=True))
    op.add_column("compute_instances", sa.Column("market_gpu_microusd", sa.BigInteger(), nullable=True))
    op.add_column("compute_instances", sa.Column("outbid_count", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "image_sizes",
        sa.Column("digest", sa.String(80), primary_key=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("checked_at", _TZ, nullable=False),
    )
    op.create_table(
        "compute_wait_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("compute_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("gpu_names", postgresql.JSONB(), nullable=False),
        sa.Column("num_gpus", sa.Integer(), nullable=False),
        sa.Column("min_gpu_ram_gb", sa.Integer(), nullable=True),
        sa.Column("rental_type", sa.String(16), nullable=False),
        sa.Column("target_microusd", sa.BigInteger(), nullable=False),
        sa.Column("max_microusd", sa.BigInteger(), nullable=False),
        sa.Column("limit_microusd", sa.BigInteger(), nullable=True),
        sa.Column("started_at", _TZ, nullable=False),
        sa.Column("ended_at", _TZ, nullable=False),
        sa.Column("waited_seconds", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("rented_microusd", sa.BigInteger(), nullable=True),
        sa.Column("cheapest_microusd", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_compute_wait_samples_job_id", "compute_wait_samples", ["job_id"])
    op.create_index("ix_compute_wait_samples_ended_at", "compute_wait_samples", ["ended_at"])
    op.create_table(
        "gpu_offer_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sampled_at", _TZ, nullable=False),
        sa.Column("gpu_name", sa.String(64), nullable=False),
        sa.Column("rental_type", sa.String(16), nullable=False),
        sa.Column("machine_id", sa.String(128), nullable=True),
        sa.Column("offer_id", sa.String(128), nullable=False),
        sa.Column("gpu_microusd", sa.BigInteger(), nullable=False),
        sa.Column("full_microusd", sa.BigInteger(), nullable=False),
        sa.Column("reliability", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_gpu_offer_observations_machine",
        "gpu_offer_observations",
        ["gpu_name", "rental_type", "machine_id", "sampled_at"],
    )
    op.create_index("ix_gpu_offer_observations_sampled_at", "gpu_offer_observations", ["sampled_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("gpu_offer_observations")
    op.drop_table("compute_wait_samples")
    op.drop_table("image_sizes")
    for name in (
        "outbid_count",
        "market_gpu_microusd",
        "charged_items",
        "est_transfer_out_microusd",
        "est_transfer_in_microusd",
        "est_image_microusd",
        "est_disk_microusd",
        "est_gpu_microusd",
        "net_reported_at",
        "net_priced_out_bytes",
        "net_priced_in_bytes",
        "net_out_bytes",
        "net_in_bytes",
        "image_bytes",
        "link_up_mbps",
        "link_down_mbps",
    ):
        op.drop_column("compute_instances", name)
    op.drop_column("compute_jobs", "wait_started_at")
    op.drop_column("compute_settings", "credit_reserve_hours")
