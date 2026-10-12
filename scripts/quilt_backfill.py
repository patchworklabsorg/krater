"""Add Quilt events for every existing project to the `quilt_outbox` table.

Run it once after the first deploy of the Quilt integration (DATABASE_URL settings as for the app):

    uv run python scripts/quilt_backfill.py

It runs `krater.services.quilt_events.backfill_all`: for each project, oldest first, it adds the events
Quilt doesn't have yet. Event ids come from the source rows (uuid5), so a second run adds nothing. The
worker sends the rows; see docs/quilt-integration.md.
"""

from __future__ import annotations

import sys

from krater.db import get_sessionmaker
from krater.services.quilt_events import backfill_all


def main() -> int:
    session = get_sessionmaker()()
    try:
        added = backfill_all(session)
        session.commit()
    finally:
        session.close()
    print(f"quilt backfill: added {added} outbox rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
