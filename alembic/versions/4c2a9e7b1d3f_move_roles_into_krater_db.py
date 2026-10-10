"""move roles into krater's database

Revision ID: 4c2a9e7b1d3f
Revises: 29fc5db971e5
Create Date: 2026-09-28 10:00:00.000000

By maintainer decision, Krater now runs against Weave's main branch (plain OIDC sign-in, no `groups`
claim, no directory API), so roles live in Krater's own database. Hand-written, then checked against
`alembic revision --autogenerate` (which reports no diff after this runs):

* `user_roles` and `pending_role_grants` are new (see `krater.models.user_role`).
* `users.groups_cached` (the last-seen Weave `groups` claim) seeds `user_roles` (only `ganymede:*`
  values), with one `role_grant` audit event per seeded row, and is then dropped. `downgrade()`
  rebuilds it from `user_roles`.
* `users.disabled_at` and `users.email_verified` are new. Existing rows start unverified; the next
  sign-in sets it from the id_token.
* `users.slack_user_id` becomes unique, so a Slack click resolves to one user. Any duplicates are
  cleared first, keeping the most recently signed-in user's copy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4c2a9e7b1d3f"
down_revision: str | Sequence[str] | None = "29fc5db971e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
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

    op.add_column("users", sa.Column("email_verified", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("users", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        """
        INSERT INTO user_roles (id, user_id, role, granted_by_id, created_at)
        SELECT gen_random_uuid(), u.id, g.role, NULL, now()
        FROM users u CROSS JOIN LATERAL unnest(u.groups_cached) AS g(role)
        WHERE g.role LIKE 'ganymede:%'
        ON CONFLICT (user_id, role) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO audit_events (id, actor_id, action, project_id, payload, reason, created_at)
        SELECT gen_random_uuid(), NULL, 'role_grant', NULL,
               jsonb_build_object('user_id', ur.user_id::text, 'role', ur.role),
               'migrated from the Weave groups claim', now()
        FROM user_roles ur
        """
    )
    op.drop_column("users", "groups_cached")

    op.execute(
        """
        UPDATE users SET slack_user_id = NULL
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY slack_user_id ORDER BY last_login_at DESC NULLS LAST, created_at DESC
                ) AS rn
                FROM users WHERE slack_user_id IS NOT NULL
            ) ranked
            WHERE rn > 1
        )
        """
    )
    op.create_unique_constraint(op.f("uq_users_slack_user_id"), "users", ["slack_user_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("uq_users_slack_user_id"), "users", type_="unique")
    op.add_column(
        "users",
        sa.Column("groups_cached", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
    )
    op.execute(
        """
        UPDATE users SET groups_cached = COALESCE(
            (SELECT array_agg(ur.role ORDER BY ur.role) FROM user_roles ur WHERE ur.user_id = users.id),
            '{}'
        )
        """
    )
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "email_verified")
    op.drop_index(op.f("ix_pending_role_grants_email"), table_name="pending_role_grants")
    op.drop_table("pending_role_grants")
    op.drop_index(op.f("ix_user_roles_user_id"), table_name="user_roles")
    op.drop_table("user_roles")
