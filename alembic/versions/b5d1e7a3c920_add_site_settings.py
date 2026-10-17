"""add site settings

Revision ID: b5d1e7a3c920
Revises: a7c3e91f0b42
Create Date: 2026-10-14 18:00:00.000000

`site_settings`: one row of site-wide settings admins change on /admin. The first is how the public gallery names
team members under 18 (`first`: their first name only, until an admin chooses otherwise).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5d1e7a3c920"
down_revision: str | Sequence[str] | None = "a7c3e91f0b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    site_settings = op.create_table(
        "site_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "gallery_minor_names",
            sa.Enum(
                "full",
                "first",
                "hidden",
                name="gallery_minor_names",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            server_default="first",
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_site_settings_singleton")),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], name=op.f("fk_site_settings_updated_by_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_settings")),
    )
    op.bulk_insert(site_settings, [{"id": 1, "gallery_minor_names": "first"}])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("site_settings")
