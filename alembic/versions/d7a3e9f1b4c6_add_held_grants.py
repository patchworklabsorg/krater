"""add held_grants: budget grants waiting for the team's verification in Weave

Revision ID: d7a3e9f1b4c6
Revises: c4f7a9e2d815
Create Date: 2026-11-06 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a3e9f1b4c6"
down_revision: str | Sequence[str] | None = "c4f7a9e2d815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "held_grants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "initial_approval",
                "amendment",
                "admin_adjustment",
                "reclaim",
                name="budget_entry_kind",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("revision_id", sa.UUID(), sa.ForeignKey("project_revisions.id"), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_entry_id", sa.UUID(), sa.ForeignKey("budget_entries.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_held_grants_project_id", "held_grants", ["project_id"])
    op.create_index(
        "uq_held_grants_one_pending",
        "held_grants",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL AND cancelled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_held_grants_one_pending", table_name="held_grants")
    op.drop_index("ix_held_grants_project_id", table_name="held_grants")
    op.drop_table("held_grants")
