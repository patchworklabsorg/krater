"""`krater.storage.ensure_bucket`: the deploy-time entry point the `migrate` service runs."""

from __future__ import annotations

import pytest

from krater.config import Settings
from krater.storage import ensure_bucket
from krater.storage.errors import StorageUnavailableError


class _FlakyStore:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def ensure_bucket(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise StorageUnavailableError("not up yet")


def _patch(monkeypatch: pytest.MonkeyPatch, settings: Settings, store: _FlakyStore | None) -> list:
    monkeypatch.setattr(ensure_bucket, "get_settings", lambda: settings)
    buckets: list = []

    def _build(_settings: Settings, *, bucket: str | None = None) -> _FlakyStore:
        if store is None:
            raise AssertionError("no store should be built in fake mode")
        buckets.append(bucket)
        return store

    monkeypatch.setattr(ensure_bucket, "S3ObjectStore", _build)
    return buckets


def test_skips_in_fake_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, Settings(s3_mode="fake"), None)
    ensure_bucket.main(delay_seconds=0)


def test_retries_until_storage_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FlakyStore(failures=2)
    buckets = _patch(monkeypatch, Settings(s3_mode="live"), store)
    ensure_bucket.main(attempts=5, delay_seconds=0)
    # Two failed attempts, then the screenshot bucket and the compute bucket.
    assert store.calls == 4
    assert buckets == [None, "krater-compute"]


def test_gives_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FlakyStore(failures=10)
    _patch(monkeypatch, Settings(s3_mode="live"), store)
    with pytest.raises(StorageUnavailableError):
        ensure_bucket.main(attempts=3, delay_seconds=0)
    assert store.calls == 3
