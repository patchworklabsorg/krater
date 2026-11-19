"""Saying, at most once a day, that a newer `krater` is out. Krater sends the newest version it knows in a header on
every API answer (`X-Krater-CLI-Latest`), so this asks nothing of its own. Printed to stderr, so `--json` output
stays clean; `KRATER_NO_UPDATE_NOTICE=1` turns it off."""

from __future__ import annotations

import json
import os
import re
import sys
import time

from krater_cli import __version__, store

HEADER = "X-Krater-CLI-Latest"
EVERY_SECONDS = 24 * 3600
UPGRADE = 'uv tool install --force "git+https://github.com/patchworklabsorg/krater#subdirectory=cli"'
_said = False


def _parts(version: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(?:\.\d+)*", version or ""):
        return None
    return tuple(int(part) for part in version.split("."))


def is_newer(latest: str | None, current: str = __version__) -> bool:
    theirs, ours = _parts(latest or ""), _parts(current)
    return theirs is not None and ours is not None and theirs > ours


def notice(latest: str | None, *, now: float | None = None) -> None:
    global _said
    if _said or os.environ.get("KRATER_NO_UPDATE_NOTICE") or not is_newer(latest):
        return
    _said = True
    now = time.time() if now is None else now
    path = store.config_dir() / "update-notice.json"
    try:
        last = json.loads(path.read_text(encoding="utf-8")).get("at", 0)
    except (OSError, ValueError, AttributeError):
        last = 0
    if now - last < EVERY_SECONDS:
        return
    print(
        f"krater: version {latest} is out (you have {__version__}). Update with: {UPGRADE}",
        file=sys.stderr,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": now, "version": latest}), encoding="utf-8")
    except OSError:
        pass
