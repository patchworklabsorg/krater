"""The `ObjectStore` protocol every adapter (live or fake) implements.

Nothing outside `krater.storage` should know the storage backend's endpoints, credentials or wire
format -- go through this interface. See `docs/dev/storage.md` for the SeaweedFS container this backs
in dev, and `docs/SPEC.md` ("Completion flow & public gallery") for why screenshots live here at all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from krater.storage.types import ListedObject, MultipartUpload, ObjectMeta, PresignedPost

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

    def read_tail(self, key: str, n: int) -> bytes | None:
        """The last `n` bytes of `key` (a suffix-ranged `GET`), or `None` if no such object exists. Used to show the
        end of a job's log without fetching all of it."""
        ...

    def delete(self, key: str) -> None:
        """Delete `key`. Safe to call on a key that's already gone (S3's own `DeleteObject` semantics)."""
        ...

    # -- Machine uploads (Krater Compute checkpoints and outputs) ---------------------------------------
    # A rented machine gets only presigned URLs, never storage credentials. Uploads bigger than one part go up
    # as multipart uploads, so no single request body passes Cloudflare's cap in front of the storage host.

    def presign_put(self, key: str, *, content_length: int, expires: int = DEFAULT_UPLOAD_EXPIRES_SECONDS) -> str:
        """A presigned `PUT` URL for uploading exactly `content_length` bytes to `key` in one request. The length is
        signed into the URL, so storage refuses a body of any other size."""
        ...

    def list_prefix(self, prefix: str) -> list[ListedObject]:
        """Every object whose key starts with `prefix`."""
        ...

    def create_multipart_upload(self, key: str) -> str:
        """Start a multipart upload of `key`; returns its upload id."""
        ...

    def presign_upload_part(
        self,
        key: str,
        *,
        upload_id: str,
        part_number: int,
        content_length: int,
        expires: int = DEFAULT_UPLOAD_EXPIRES_SECONDS,
    ) -> str:
        """A presigned `PUT` URL for one part (numbered from 1) of a multipart upload, of exactly
        `content_length` bytes."""
        ...

    def complete_multipart_upload(self, key: str, *, upload_id: str, parts: list[tuple[int, str]]) -> None:
        """Finish a multipart upload from its `(part number, ETag)` pairs."""
        ...

    def list_multipart_uploads(self, prefix: str) -> list[MultipartUpload]:
        """Multipart uploads under `prefix` that were started and not finished (their parts take space)."""
        ...

    def abort_multipart_upload(self, key: str, *, upload_id: str) -> None:
        """Abort an unfinished multipart upload and free its parts."""
        ...


__all__ = ["DEFAULT_DOWNLOAD_EXPIRES_SECONDS", "DEFAULT_UPLOAD_EXPIRES_SECONDS", "ObjectStore"]
