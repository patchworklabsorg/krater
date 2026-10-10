"""Manual/debugging entry point: run one SkyPilot reconcile pass and exit.

    uv run python -m krater.skypilot.reconcile_once

Does exactly what the worker's periodic `skypilot_reconcile` task does, on demand -- useful for testing
against a real SkyPilot server or checking a suspected budget/workspace drift without waiting for the
next scheduled tick.
"""

from __future__ import annotations

import logging

from krater.config import get_settings
from krater.db import get_sessionmaker
from krater.services.skypilot_sync import reconcile
from krater.skypilot import get_skypilot_client
from krater.weave import get_weave_client


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    session = get_sessionmaker()()
    try:
        reconcile(
            session, get_skypilot_client(), get_weave_client(), warn_percent=settings.skypilot_budget_warn_percent
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
