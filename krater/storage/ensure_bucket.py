"""Deploy-time entry point: make sure the screenshot bucket exists and has its CORS rule, then exit.

    uv run python -m krater.storage.ensure_bucket

`docker-compose.yml`'s one-shot `migrate` service runs this right after `alembic upgrade head`, so the
bucket is ready before `portal`/`worker` start. Does nothing with `KRATER_S3_MODE=fake`. Retries while
the `storage` container is still starting. See `docs/dev/storage.md`.
"""

from __future__ import annotations

import logging
import time

from krater.config import get_settings
from krater.storage.errors import StorageUnavailableError
from krater.storage.live import S3ObjectStore

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 30
DEFAULT_DELAY_SECONDS = 2.0


def main(*, attempts: int = DEFAULT_ATTEMPTS, delay_seconds: float = DEFAULT_DELAY_SECONDS) -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if settings.s3_mode != "live":
        logger.info("KRATER_S3_MODE=%s: no bucket to set up", settings.s3_mode)
        return

    store = S3ObjectStore(settings)
    for attempt in range(1, attempts + 1):
        try:
            store.ensure_bucket()
        except StorageUnavailableError as exc:
            if attempt == attempts:
                raise
            logger.warning("storage not ready (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(delay_seconds)
        else:
            logger.info("bucket %r is ready", settings.s3_bucket)
            return


if __name__ == "__main__":
    main()
