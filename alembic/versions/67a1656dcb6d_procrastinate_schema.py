"""procrastinate schema

Revision ID: 67a1656dcb6d
Revises: 59b55982f8a6
Create Date: 2026-09-26 19:06:29.266229

procrastinate ships its schema as raw SQL (`procrastinate.sql.schema.sql`) and normally applies it
itself (`procrastinate schema --apply`, or a job store's `apply_schema()`). We use Alembic as the only
migration path instead, so this migration pulls that same SQL from the installed `procrastinate`
package via `procrastinate.schema.SchemaManager.get_schema()` -- the approach procrastinate's own docs
point to for projects that manage migrations with a different tool -- and executes it here. Bump this
migration (or add a new one) if `procrastinate` is upgraded to a version with schema changes.

`downgrade()` can't reuse a procrastinate helper the same way (it doesn't ship a "drop everything"
function), so it drops procrastinate's tables, functions and types explicitly. Functions are dropped
by matching `procrastinate_%` in `pg_proc` rather than by listing exact signatures, since those change
between procrastinate versions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from procrastinate.schema import SchemaManager

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "67a1656dcb6d"
down_revision: str | Sequence[str] | None = "59b55982f8a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROP_FUNCTIONS_SQL = """
DO $$
DECLARE
    proc RECORD;
BEGIN
    FOR proc IN
        SELECT p.oid::regprocedure AS signature
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%' ESCAPE '\\'
    LOOP
        EXECUTE 'DROP FUNCTION IF EXISTS ' || proc.signature || ' CASCADE';
    END LOOP;
END;
$$;
"""

_TABLES = (
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
    "procrastinate_workers",
)

_TYPES = (
    "procrastinate_job_status",
    "procrastinate_job_event_type",
    "procrastinate_job_to_defer_v1",
)


def upgrade() -> None:
    """Upgrade schema."""
    # `op.execute` wraps a plain string in `sa.text(...)` itself; checked that this SQL has no bare
    # `:name`-style substrings that `text()` would otherwise mistake for bind parameters.
    op.execute(SchemaManager.get_schema())


def downgrade() -> None:
    """Downgrade schema."""
    for table in _TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    op.execute(sa.text(_DROP_FUNCTIONS_SQL))
    for type_name in _TYPES:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {type_name} CASCADE"))
