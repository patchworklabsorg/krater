"""add quilt outbox

Revision ID: 83af68b415ca
Revises: 5f3b46edeb95
Create Date: 2026-10-10 16:06:56.374626

`quilt_outbox` holds the events Krater sends to Quilt's patch API. The services write a row in the same
transaction as the change it reports, and the worker sends the rows. See docs/quilt-integration.md.

The `state` column's `sa.Enum(..., create_constraint=True)` attaches its own CHECK constraint, so there is
no separate `sa.CheckConstraint` here (see the initial schema migration).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "83af68b415ca"
down_revision: str | Sequence[str] | None = "5f3b46edeb95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "quilt_outbox",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                "skipped",
                name="quilt_outbox_state",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quilt_outbox")),
        sa.UniqueConstraint("seq", name=op.f("uq_quilt_outbox_seq")),
    )
    op.create_index("ix_quilt_outbox_external_id_seq", "quilt_outbox", ["external_id", "seq"], unique=False)
    op.create_index("ix_quilt_outbox_state_seq", "quilt_outbox", ["state", "seq"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_quilt_outbox_state_seq", table_name="quilt_outbox")
    op.drop_index("ix_quilt_outbox_external_id_seq", table_name="quilt_outbox")
    op.drop_table("quilt_outbox")
