"""A tiny in-process TTL cache, used to absorb bursts of directory lookups (e.g. Slack button clicks)."""

from __future__ import annotations

import time

#: Sentinel distinguishing "not cached" from a cached `None` (a directory miss is worth caching too).
MISSING = object()


class TTLCache[K, V]:
    """A `dict`-like cache where each entry expires `ttl_seconds` after it was set. Not thread-safe;
    fine for the single-worker-per-request model this app runs under."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | object:
        """The cached value for `key`, or `MISSING` if absent or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return MISSING
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._entries[key]
            return MISSING
        return value

    def set(self, key: K, value: V) -> None:
        self._entries[key] = (time.monotonic() + self._ttl_seconds, value)
