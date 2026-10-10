"""Data carried out of `krater.storage`. See `krater/storage/client.py` for how each is produced."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PresignedPost:
    """A presigned S3 `POST` policy: the browser posts the file straight to `url`, with `fields` sent
    as form fields alongside it (in any order, before the file field itself).

    Carries a `content-length-range` condition and an exact `Content-Type` match baked into the signed
    policy, so storage itself enforces the size limit -- the server never has to stream the upload
    through itself just to reject an oversized file.
    """

    url: str
    fields: dict[str, str]


@dataclass(frozen=True)
class ObjectMeta:
    """What `ObjectStore.head` reports about an object that exists."""

    size_bytes: int
    content_type: str | None


__all__ = ["ObjectMeta", "PresignedPost"]
