"""`krater.storage`: the only place that talks to object storage (gallery screenshots).

Nothing outside this package should import `boto3`/know the bucket's endpoints or credentials -- go
through `ObjectStore` (via `get_object_store`). See `docs/dev/storage.md` for the SeaweedFS container
this backs in dev, and `docs/SPEC.md` ("Completion flow & public gallery") for the feature.
"""

from __future__ import annotations

from functools import lru_cache

from krater.config import get_settings
from krater.storage.client import ObjectStore
from krater.storage.errors import StorageError, StorageUnavailableError
from krater.storage.fake import FakeObjectStore
from krater.storage.live import S3ObjectStore
from krater.storage.types import ObjectMeta, PresignedPost

__all__ = [
    "FakeObjectStore",
    "ObjectMeta",
    "ObjectStore",
    "PresignedPost",
    "S3ObjectStore",
    "StorageError",
    "StorageUnavailableError",
    "get_object_store",
]


@lru_cache
def get_object_store() -> ObjectStore:
    """The process-wide `ObjectStore`, chosen by `settings.s3_mode`.

    Cached like `krater.skypilot.get_skypilot_client`; override in tests by passing a `FakeObjectStore`
    directly to the service functions instead of going through this factory.
    """
    settings = get_settings()
    if settings.s3_mode == "fake":
        return FakeObjectStore()
    return S3ObjectStore(settings)
