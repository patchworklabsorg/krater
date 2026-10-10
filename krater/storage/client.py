"""The `ObjectStore` protocol every adapter (live or fake) implements.

Nothing outside `krater.storage` should know the storage backend's endpoints, credentials or wire
format -- go through this interface. See `docs/dev/storage.md` for the SeaweedFS container this backs
in dev, and `docs/SPEC.md` ("Completion flow & public gallery") for why screenshots live here at all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from krater.storage.types import ObjectMeta, PresignedPost

#: Default lifetime for a presigned upload/download URL. Short: these are handed to a browser and used
#: (or not) within the same page load.
DEFAULT_UPLOAD_EXPIRES_SECONDS = 300
DEFAULT_DOWNLOAD_EXPIRES_SECONDS = 3600


@runtime_checkable
class ObjectStore(Protocol):
    def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires: int = DEFAULT_UPLOAD_EXPIRES_SECONDS
    ) -> PresignedPost:
        """A presigned `POST` policy for uploading directly to `key`.

        The policy pins both the object's `Content-Type` (to exactly `content_type`) and its size (via
        a `content-length-range` condition, `1..max_bytes`) into the signature, so storage itself
        rejects an upload that doesn't match either -- the caller never has to stream the bytes through
        itself just to enforce them. `key` must already be the server-generated key the caller intends
        to store the object under; this protocol never chooses one.
        """
        ...

    def presign_download(self, key: str, *, expires: int = DEFAULT_DOWNLOAD_EXPIRES_SECONDS) -> str:
        """A short-lived, presigned `GET` URL for `key`. Doesn't check the object exists."""
        ...

    def head(self, key: str) -> ObjectMeta | None:
        """`key`'s size and declared content type, or `None` if no such object exists."""
        ...

    def read_prefix(self, key: str, n: int) -> bytes | None:
        """The first `n` bytes of `key` (a ranged `GET`, not a full download), or `None` if no such
        object exists. Used to check a file's magic-byte signature without fetching the whole object."""
        ...

    def delete(self, key: str) -> None:
        """Delete `key`. Safe to call on a key that's already gone (S3's own `DeleteObject` semantics)."""
        ...


__all__ = ["DEFAULT_DOWNLOAD_EXPIRES_SECONDS", "DEFAULT_UPLOAD_EXPIRES_SECONDS", "ObjectStore"]
