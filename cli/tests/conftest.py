from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """Every test signs in to a folder of its own, never the real one (`%APPDATA%/krater`, `~/.config/krater`)."""
    monkeypatch.setenv("KRATER_CONFIG_DIR", str(tmp_path / "krater"))
    return tmp_path / "krater"
