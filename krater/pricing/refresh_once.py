"""Manual/debugging entry point: run one GPU-pricing refresh and exit.

    uv run python -m krater.pricing.refresh_once

Does exactly what the worker's daily `pricing_refresh` periodic task does, on demand -- useful for
seeding a fresh database or checking the source after a SkyPilot catalog schema bump without waiting
for the next scheduled run. See `docs/dev/pricing.md`.
"""

from __future__ import annotations

import logging

from krater.db import get_sessionmaker
from krater.services.pricing import refresh_prices
from krater.skypilot import get_skypilot_client

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    session = get_sessionmaker()()
    try:
        count = refresh_prices(session, get_skypilot_client())
        session.commit()
        logger.info("refreshed pricing for %d accelerator/count groups", count)
    finally:
        session.close()


if __name__ == "__main__":
    main()
