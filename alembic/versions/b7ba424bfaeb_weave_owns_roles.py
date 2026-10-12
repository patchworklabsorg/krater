"""weave owns roles

Revision ID: b7ba424bfaeb
Revises: 4c2a9e7b1d3f
Create Date: 2026-10-07 15:33:28.797985

By maintainer decision, Weave owns Krater's roles again (the `roles` claim and the directory API), so
the Krater-side role tables and disable switch from 4c2a9e7b1d3f go away:

* `users.roles_cached` is new: the Krater role names Weave last reported, for display only. It starts
  from the user's rows in `user_roles`; the next sign-in or directory lookup overwrites it.
* `user_roles` and `pending_role_grants` are dropped.
* `users.disabled_at` is dropped. Disabling someone is now done in Weave.

`users.email_verified` and the unique `users.slack_user_id` stay. Past `role_grant` and similar audit
events stay as history. `downgrade()` recreates the tables, seeds `user_roles` from `roles_cached`,
and brings back `disabled_at` (empty).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7ba424bfaeb"
down_revision: str | Sequence[str] | None = "4c2a9e7b1d3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("roles_cached", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
    )
    op.execute(
        """
        UPDATE users SET roles_cached = COALESCE(
            (SELECT array_agg(ur.role ORDER BY ur.role) FROM user_roles ur WHERE ur.user_id = users.id),
            '{}'
        )
        """
    )
    op.drop_index(op.f("ix_pending_role_grants_email"), table_name="pending_role_grants")
    op.drop_table("pending_role_grants")
    op.drop_index(op.f("ix_user_roles_user_id"), table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_column("users", "disabled_at")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("users", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("granted_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["granted_by_id"], ["users.id"], name=op.f("fk_user_roles_granted_by_id_users")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_roles")),
        sa.UniqueConstraint("user_id", "role", name="uq_user_roles_user_id_role"),
    )
    op.create_index(op.f("ix_user_roles_user_id"), "user_roles", ["user_id"], unique=False)
    op.create_table(
        "pending_role_grants",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("granted_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["granted_by_id"], ["users.id"], name=op.f("fk_pending_role_grants_granted_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_role_grants")),
        sa.UniqueConstraint("email", "role", name="uq_pending_role_grants_email_role"),
    )
    op.create_index(op.f("ix_pending_role_grants_email"), "pending_role_grants", ["email"], unique=False)
    op.execute(
        """
        INSERT INTO user_roles (id, user_id, role, granted_by_id, created_at)
        SELECT gen_random_uuid(), u.id, r.role, NULL, now()
        FROM users u CROSS JOIN LATERAL unnest(u.roles_cached) AS r(role)
        ON CONFLICT (user_id, role) DO NOTHING
        """
    )
    op.drop_column("users", "roles_cached")
