"""add project_members (project teams); drop project_revisions.credited_builder_ids

A project's team is its lead (the submitter) plus the members they invite, who join by accepting. The team
replaces per-revision credited builders: the gallery credits the team.

Revision ID: e3a9c6b2d714
Revises: b2d4f6a8c0e1
Create Date: 2026-10-12 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a9c6b2d714"
down_revision: str | Sequence[str] | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("invited_by_id", sa.UUID(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["invited_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_members_project_id_user_id"),
    )
    op.create_index(op.f("ix_project_members_project_id"), "project_members", ["project_id"])
    op.create_index(op.f("ix_project_members_user_id"), "project_members", ["user_id"])
    # Credited builders become accepted team members.
    op.execute(
        """
        INSERT INTO project_members (id, project_id, user_id, invited_by_id, accepted_at)
        SELECT DISTINCT ON (r.project_id, b.user_id) gen_random_uuid(), r.project_id, b.user_id, p.submitter_id, now()
        FROM project_revisions r
        JOIN projects p ON p.id = r.project_id
        CROSS JOIN LATERAL unnest(r.credited_builder_ids) AS b(user_id)
        WHERE b.user_id <> p.submitter_id
        """
    )
    op.drop_column("project_revisions", "credited_builder_ids")


def downgrade() -> None:
    op.add_column(
        "project_revisions",
        sa.Column(
            "credited_builder_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.drop_index(op.f("ix_project_members_user_id"), table_name="project_members")
    op.drop_index(op.f("ix_project_members_project_id"), table_name="project_members")
    op.drop_table("project_members")
