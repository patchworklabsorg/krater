"""Where the CLI keeps its sign-in: one JSON file, readable by its owner only.

`%APPDATA%\\krater\\credentials.json` on Windows (the user profile's ACL already keeps other users out),
`$XDG_CONFIG_HOME/krater/credentials.json` or `~/.config/krater/credentials.json` elsewhere (mode 0600 in a 0700
directory). `KRATER_CONFIG_DIR` overrides the directory. The file holds Weave tokens for the Krater CLI's app only:
they can do what the member can do in Krater, nothing in other apps.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def config_dir() -> Path:
    if os.environ.get("KRATER_CONFIG_DIR"):
        return Path(os.environ["KRATER_CONFIG_DIR"])
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "krater"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "krater"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def load() -> dict | None:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(data: dict) -> None:
    """Write atomically, created with mode 0600 so the tokens are never readable by others, even for a moment."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    path = credentials_path()
    # A unique name in the same directory (so the replace is atomic): two saves at once can't interleave in one file.
    # `mkstemp` creates it with mode 0600.
    fd, name = tempfile.mkstemp(dir=directory, prefix="credentials.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def clear() -> bool:
    try:
        credentials_path().unlink()
    except FileNotFoundError:
        return False
    return True
