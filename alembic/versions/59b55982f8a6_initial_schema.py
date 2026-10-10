"""initial schema

Revision ID: 59b55982f8a6
Revises:
Create Date: 2026-09-26 19:04:14.972174

Hand-reviewed after `alembic revision --autogenerate`. Two things needed fixing beyond what
autogenerate produced:

* Each `sa.Enum(..., create_constraint=True)` column already attaches its own CHECK constraint when
  the table is built (that's what `create_constraint=True` does); autogenerate additionally rendered
  an explicit `sa.CheckConstraint(...)` for the same check, twice, which duplicated the constraint and
  made `upgrade()` fail outright (`DuplicateObject`). The redundant explicit constraints are removed
  here; the column type still creates the one real constraint.
* `projects.current_revision_id` / `approved_revision_id` and `project_revisions.project_id` form a
  genuine circular FK dependency between the two tables. Autogenerate rendered both `use_alter=True`
  foreign keys inline inside `create_table('projects', ...)`, which fails because `project_revisions`
  doesn't exist yet at that point in the script. They're added below via `op.create_foreign_key(...)`
  once both tables exist, and dropped again (via `op.drop_constraint`) before either table is dropped
  in `downgrade()`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "59b55982f8a6"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "approval_policies",
        sa.Column(
            "stage",
            sa.Enum(
                "proposal", "completion", name="approval_stage", native_enum=False, create_constraint=True, length=64
            ),
            nullable=False,
        ),
        sa.Column("min_budget_cents", sa.Integer(), nullable=True),
        sa.Column("min_approvals", sa.Integer(), server_default="1", nullable=False),
        sa.Column("required_group", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_policies")),
    )
    op.create_table(
        "users",
        sa.Column("weave_sub", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("slack_user_id", sa.String(length=64), nullable=True),
        sa.Column("groups_cached", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("weave_sub", name=op.f("uq_users_weave_sub")),
    )
    op.create_table(
        "projects",
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("submitter_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "pending_review",
                "changes_requested",
                "approved",
                "pending_completion_review",
                "completion_changes_requested",
                "completed",
                "withdrawn",
                name="project_status",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            server_default="draft",
            nullable=False,
        ),
        # FKs to project_revisions.id are added below, once that table exists (circular dependency).
        sa.Column("current_revision_id", sa.UUID(), nullable=True),
        sa.Column("approved_revision_id", sa.UUID(), nullable=True),
        sa.Column("repo_url", sa.String(length=2048), nullable=True),
        sa.Column("slack_channel_id", sa.String(length=64), nullable=True),
        sa.Column("skypilot_workspace", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["submitter_id"], ["users.id"], name=op.f("fk_projects_submitter_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_index(op.f("ix_projects_submitter_id"), "projects", ["submitter_id"], unique=False)
    op.create_table(
        "audit_events",
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name=op.f("fk_audit_events_actor_id_users")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_audit_events_project_id_projects")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)
    op.create_index(op.f("ix_audit_events_actor_id"), "audit_events", ["actor_id"], unique=False)
    op.create_index(op.f("ix_audit_events_project_id"), "audit_events", ["project_id"], unique=False)
    op.create_table(
        "project_revisions",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "proposal",
                "amendment",
                "completion",
                name="revision_kind",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("write_up", sa.Text(), nullable=False),
        sa.Column("budget_requested_cents", sa.Integer(), nullable=False),
        sa.Column("demo_url", sa.String(length=2048), nullable=True),
        sa.Column("screenshot_keys", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("credited_builder_ids", postgresql.ARRAY(sa.UUID()), server_default="{}", nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "superseded",
                name="revision_outcome",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_project_revisions_project_id_projects")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_revisions")),
        sa.UniqueConstraint("project_id", "number", name="uq_project_revisions_project_id_number"),
    )
    op.create_index(op.f("ix_project_revisions_project_id"), "project_revisions", ["project_id"], unique=False)

    # Both tables now exist: add the circular FKs from `projects` to `project_revisions` via ALTER TABLE.
    op.create_foreign_key(
        op.f("fk_projects_current_revision_id_project_revisions"),
        "projects",
        "project_revisions",
        ["current_revision_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_projects_approved_revision_id_project_revisions"),
        "projects",
        "project_revisions",
        ["approved_revision_id"],
        ["id"],
    )

    op.create_table(
        "spend_snapshots",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("estimated_spend_cents", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum("skypilot_cost_report", name="spend_source", native_enum=False, create_constraint=True, length=64),
            nullable=False,
        ),
        sa.Column("taken_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_spend_snapshots_project_id_projects")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_snapshots")),
    )
    op.create_index(
        "ix_spend_snapshots_project_id_taken_at", "spend_snapshots", ["project_id", "taken_at"], unique=False
    )
    op.create_table(
        "budget_entries",
        sa.Column("project_id", sa.UUID(), nullable=False),
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
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("revision_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name=op.f("fk_budget_entries_actor_id_users")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_budget_entries_project_id_projects")),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["project_revisions.id"], name=op.f("fk_budget_entries_revision_id_project_revisions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_entries")),
    )
    op.create_index(op.f("ix_budget_entries_actor_id"), "budget_entries", ["actor_id"], unique=False)
    op.create_index(op.f("ix_budget_entries_project_id"), "budget_entries", ["project_id"], unique=False)
    op.create_index(op.f("ix_budget_entries_revision_id"), "budget_entries", ["revision_id"], unique=False)
    op.create_table(
        "reviews",
        sa.Column("revision_id", sa.UUID(), nullable=False),
        sa.Column("reviewer_id", sa.UUID(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("approve", "reject", name="review_decision", native_enum=False, create_constraint=True, length=64),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.Enum("slack", "web", name="review_source", native_enum=False, create_constraint=True, length=64),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], name=op.f("fk_reviews_reviewer_id_users")),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["project_revisions.id"], name=op.f("fk_reviews_revision_id_project_revisions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
    )
    op.create_index(op.f("ix_reviews_reviewer_id"), "reviews", ["reviewer_id"], unique=False)
    op.create_index(op.f("ix_reviews_revision_id"), "reviews", ["revision_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_reviews_revision_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_reviewer_id"), table_name="reviews")
    op.drop_table("reviews")
    op.drop_index(op.f("ix_budget_entries_revision_id"), table_name="budget_entries")
    op.drop_index(op.f("ix_budget_entries_project_id"), table_name="budget_entries")
    op.drop_index(op.f("ix_budget_entries_actor_id"), table_name="budget_entries")
    op.drop_table("budget_entries")
    op.drop_index("ix_spend_snapshots_project_id_taken_at", table_name="spend_snapshots")
    op.drop_table("spend_snapshots")

    # Drop the circular FKs before either table goes away.
    op.drop_constraint(op.f("fk_projects_approved_revision_id_project_revisions"), "projects", type_="foreignkey")
    op.drop_constraint(op.f("fk_projects_current_revision_id_project_revisions"), "projects", type_="foreignkey")

    op.drop_index(op.f("ix_project_revisions_project_id"), table_name="project_revisions")
    op.drop_table("project_revisions")
    op.drop_index(op.f("ix_audit_events_project_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_actor_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_projects_submitter_id"), table_name="projects")
    op.drop_table("projects")
    op.drop_table("users")
    op.drop_table("approval_policies")
