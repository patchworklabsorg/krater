"""add compute settings

Revision ID: f736452b1d88
Revises: 83af68b415ca
Create Date: 2026-10-11 06:00:00.000000

Krater Compute's policy numbers:

- `compute_settings`: the global policy numbers, one row (`id = 1`), seeded with the values Adam and Jasper
  approved on 2026-10-11 (reliability floor 0.95, bid margin 10%, pause grace 5 min, move after 30 min, box idle
  30 min, destroy after 24 h, reconciliation alert at 10%).
- `project_compute_settings`: per-project overrides, every number nullable (null = the global default).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f736452b1d88"
down_revision: str | Sequence[str] | None = "83af68b415ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_COLUMNS = (
    "reliability_floor_bp",
    "bid_margin_percent",
    "pause_grace_minutes",
    "move_after_minutes",
    "box_idle_minutes",
    "box_destroy_hours",
    "reconcile_alert_percent",
)


def upgrade() -> None:
    """Upgrade schema."""
    compute_settings = op.create_table(
        "compute_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        *(sa.Column(name, sa.Integer(), nullable=False) for name in _POLICY_COLUMNS),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_compute_settings_singleton")),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], name=op.f("fk_compute_settings_updated_by_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compute_settings")),
    )
    op.bulk_insert(
        compute_settings,
        [
            {
                "id": 1,
                "reliability_floor_bp": 9500,
                "bid_margin_percent": 10,
                "pause_grace_minutes": 5,
                "move_after_minutes": 30,
                "box_idle_minutes": 30,
                "box_destroy_hours": 24,
                "reconcile_alert_percent": 10,
            }
        ],
    )

    op.create_table(
        "project_compute_settings",
        sa.Column("project_id", sa.UUID(), nullable=False),
        *(sa.Column(name, sa.Integer(), nullable=True) for name in _POLICY_COLUMNS),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_compute_settings_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["users.id"], name=op.f("fk_project_compute_settings_updated_by_id_users")
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_project_compute_settings")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("project_compute_settings")
    op.drop_table("compute_settings")
