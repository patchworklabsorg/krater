"""Re-encrypt every project secret with the newest key, so an older one can be retired. Run where the portal runs:

    python -m krater.rotate_project_secrets

Rotating a key: put a new key on the first line of the key file (keep the old one below it), restart the portal,
run this, then remove the old line and restart again. Secrets no key can read are listed and left as they are.
"""

from __future__ import annotations

import sys

from krater.config import get_settings
from krater.db import get_sessionmaker
from krater.services import project_secrets


def main() -> None:
    session = get_sessionmaker()()
    try:
        done, unreadable = project_secrets.rotate_all(session, get_settings())
        session.commit()
    finally:
        session.close()
    print(f"Re-encrypted {done} project secrets with the newest key.")
    if unreadable:
        print("No key could read these; ask their teams to set them again:", *unreadable, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    main()
