"""add compute_workspaces, box save reports, and End box

A box's /workspace becomes a row of its own (`compute_workspaces`) that later boxes can continue; every existing box
gets one, pointing at the files it already has (its per-job path), so nothing moves. Box instances record each save of
/workspace and the last failure, and a job records when its team asked to end it after a save.

Revision ID: d5a8c1f3e7b9
Revises: e8b3c6d2f4a1
Create Date: 2026-11-18 03:30:00
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d5a8c1f3e7b9"
down_revision = "e8b3c6d2f4a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compute_workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prefix", sa.String(length=500), nullable=False),
        sa.Column("origin_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_compute_workspaces_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_compute_workspaces_created_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by_id"], ["users.id"], name=op.f("fk_compute_workspaces_deleted_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compute_workspaces")),
        sa.UniqueConstraint("prefix", name=op.f("uq_compute_workspaces_prefix")),
        sa.UniqueConstraint("origin_job_id", name=op.f("uq_compute_workspaces_origin_job_id")),
    )
    op.create_index(
        "ix_compute_workspaces_project_id_created_at", "compute_workspaces", ["project_id", "created_at"], unique=False
    )

    op.add_column("compute_jobs", sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("compute_jobs", sa.Column("end_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_compute_jobs_workspace_id"), "compute_jobs", ["workspace_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_compute_jobs_workspace_id_compute_workspaces"),
        "compute_jobs",
        "compute_workspaces",
        ["workspace_id"],
        ["id"],
    )

    for name, column in (
        ("workspace_saved_at", sa.DateTime(timezone=True)),
        ("workspace_files", sa.Integer()),
        ("workspace_bytes", sa.BigInteger()),
        ("last_save_error", sa.Text()),
        ("last_save_error_at", sa.DateTime(timezone=True)),
        ("save_failure_notified_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("compute_instances", sa.Column(name, column, nullable=True))
    op.add_column("compute_instances", sa.Column("save_failures", sa.Integer(), server_default="0", nullable=False))

    # Every existing box gets its workspace: its own per-job files, last saved when its last stop-save was confirmed.
    op.execute(
        """
        INSERT INTO compute_workspaces (id, project_id, name, created_by_id, prefix, origin_job_id, last_saved_at,
                                        created_at, updated_at)
        SELECT gen_random_uuid(), j.project_id, j.name, j.submitter_id,
               'projects/' || j.project_id || '/jobs/' || j.id || '/workspace/', j.id,
               (SELECT max(i.last_synced_at) FROM compute_instances i WHERE i.job_id = j.id),
               j.created_at, now()
        FROM compute_jobs j
        WHERE j.kind = 'box'
        """
    )
    op.execute(
        """
        UPDATE compute_jobs SET workspace_id = w.id
        FROM compute_workspaces w
        WHERE w.origin_job_id = compute_jobs.id AND compute_jobs.workspace_id IS NULL
        """
    )


def downgrade() -> None:
    for name in (
        "save_failures",
        "save_failure_notified_at",
        "last_save_error_at",
        "last_save_error",
        "workspace_bytes",
        "workspace_files",
        "workspace_saved_at",
    ):
        op.drop_column("compute_instances", name)
    op.drop_constraint(op.f("fk_compute_jobs_workspace_id_compute_workspaces"), "compute_jobs", type_="foreignkey")
    op.drop_index(op.f("ix_compute_jobs_workspace_id"), table_name="compute_jobs")
    op.drop_column("compute_jobs", "end_requested_at")
    op.drop_column("compute_jobs", "workspace_id")
    op.drop_index("ix_compute_workspaces_project_id_created_at", table_name="compute_workspaces")
    op.drop_table("compute_workspaces")
