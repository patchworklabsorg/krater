"""add compute_events.seq: the order events were recorded in

Revision ID: e8a4c1f7b3d9
Revises: d7c3e9a1b5f2
Create Date: 2026-11-13 01:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "e8a4c1f7b3d9"
down_revision = "d7c3e9a1b5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every event a scheduler pass records has the time the pass began, which can be before the job it handles was
    # queued, so `at` left a job's events in the wrong order on its page. New events are numbered as they're
    # inserted; existing ones in the order the pages showed them, by `at` (then id, as good as any for a tie).
    op.execute("CREATE SEQUENCE IF NOT EXISTS compute_events_seq")
    op.add_column(
        "compute_events",
        sa.Column("seq", sa.BigInteger(), server_default=sa.text("nextval('compute_events_seq')"), nullable=False),
    )
    op.execute("ALTER SEQUENCE compute_events_seq OWNED BY compute_events.seq")
    op.execute(
        "UPDATE compute_events SET seq = ordered.n FROM "
        "(SELECT id, row_number() OVER (ORDER BY at, id) AS n FROM compute_events) AS ordered "
        "WHERE compute_events.id = ordered.id"
    )
    op.execute("SELECT setval('compute_events_seq', coalesce((SELECT max(seq) FROM compute_events), 0) + 1, false)")


def downgrade() -> None:
    op.drop_column("compute_events", "seq")
    op.execute("DROP SEQUENCE IF EXISTS compute_events_seq")
