"""Exception types raised by `krater.storage`.

Mirrors `krater.skypilot.errors`: one error type for "storage couldn't be reached or failed
unexpectedly". There's no "understood but rejected" counterpart here the way SkyPilot has one, because
the two calls that can fail this way (`head`, `delete`) don't have a meaningful business-rejection case
of their own -- a missing object is a normal `None`/no-op, not an error.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every error raised by `krater.storage`."""


class StorageUnavailableError(StorageError):
    """Object storage couldn't be reached, or returned an unexpected failure."""


__all__ = ["StorageError", "StorageUnavailableError"]
