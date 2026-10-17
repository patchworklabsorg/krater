"""stop storing member data from Weave (patchworklabsorg/krater#7)

Drops `users.display_name`, `users.email` and `users.email_verified`: Krater reads names and emails from
Weave's directory when it needs them. Audit events gain `actor_sub`, filled in for existing rows, and lose the
emails earlier SkyPilot access-removal events carried. The emails in the unused `projects.skypilot_allowed_users`
are cleared.

Revision ID: a7c1e5d9f2b4
Revises: 83af68b415ca
Create Date: 2026-10-12 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c1e5d9f2b4"
down_revision: str | Sequence[str] | None = "83af68b415ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("actor_sub", sa.String(length=64), nullable=True))
    op.execute("UPDATE audit_events SET actor_sub = users.weave_sub FROM users WHERE audit_events.actor_id = users.id")
    op.execute("UPDATE audit_events SET payload = payload - 'email' WHERE payload ? 'email'")
    # Krater Compute drops this column; on a database that has gone that way first there's nothing to clear.
    if "skypilot_allowed_users" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}:
        op.execute("UPDATE projects SET skypilot_allowed_users = NULL")
    op.drop_column("users", "email_verified")
    op.drop_column("users", "email")
    op.drop_column("users", "display_name")


def downgrade() -> None:
    # The names and emails are gone; a downgrade gets placeholders until each member signs in again.
    op.add_column("users", sa.Column("display_name", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("users", sa.Column("email", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE users SET display_name = weave_sub")
    op.drop_column("audit_events", "actor_sub")
