"""add stash_archives: the URLs of each submission that Krater asks Stash to archive

Revision ID: ebee8a6d6d16
Revises: b7e2c9d4f1a8
Create Date: 2026-11-08 12:00:00.000000

`stash_archives` holds one row per project and URL. The services write a row in the same transaction as the
submission, and the worker asks Stash to archive it. See docs/stash-integration.md.

The `state` column's `sa.Enum(..., create_constraint=True)` attaches its own CHECK constraint, so there is
no separate `sa.CheckConstraint` here (see the initial schema migration).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ebee8a6d6d16"
down_revision: str | Sequence[str] | None = "b7e2c9d4f1a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "stash_archives",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("revision_id", sa.UUID(), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "archived",
                "failed",
                "skipped",
                name="stash_archive_state",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("archive_id", sa.String(length=64), nullable=True),
        sa.Column("page_url", sa.String(length=2048), nullable=True),
        sa.Column("archive_status", sa.String(length=32), nullable=True),
        sa.Column("archive_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_stash_archives_project_id_projects")),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["project_revisions.id"], name=op.f("fk_stash_archives_revision_id_project_revisions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stash_archives")),
        sa.UniqueConstraint("project_id", "url", name="uq_stash_archives_project_id_url"),
        sa.UniqueConstraint("seq", name=op.f("uq_stash_archives_seq")),
    )
    op.create_index(op.f("ix_stash_archives_project_id"), "stash_archives", ["project_id"], unique=False)
    op.create_index("ix_stash_archives_state_seq", "stash_archives", ["state", "seq"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_stash_archives_state_seq", table_name="stash_archives")
    op.drop_index(op.f("ix_stash_archives_project_id"), table_name="stash_archives")
    op.drop_table("stash_archives")
