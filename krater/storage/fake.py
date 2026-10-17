"""`FakeObjectStore`: an in-memory `ObjectStore` for `KRATER_S3_MODE=fake` (development and tests).

Makes no network calls. Keeps just enough bookkeeping (size + content type + a byte prefix per key) for
`krater.services.screenshots` to exercise the real confirm/head/read_prefix/delete flow, including its
magic-byte signature check. A test that wants to simulate "the browser successfully uploaded through the
presigned POST" calls `put` directly, mirroring how `FakeVast.set_status` simulates a fact a
real server would otherwise report.
"""

from __future__ import annotations

import itertools

from krater.storage.types import ListedObject, MultipartUpload, ObjectMeta, PresignedPost


class FakeObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, ObjectMeta] = {}
        self._content: dict[str, bytes] = {}
        #: upload id -> key, for multipart uploads started and not yet completed.
        self.multipart_uploads: dict[str, str] = {}
        self._upload_ids = itertools.count(1)

    # -- ObjectStore protocol ------------------------------------------------------------------------

    def presign_upload(self, key: str, *, content_type: str, max_bytes: int, expires: int = 300) -> PresignedPost:
        del expires
        # A fake URL, never actually POSTed to in tests -- callers that need to simulate a completed
        # upload use `put` below instead. The fields still carry the real policy inputs so a test can
        # assert on them if it wants to.
        return PresignedPost(
            url=f"fake://krater-storage/{key}",
            fields={"key": key, "Content-Type": content_type, "x-fake-max-bytes": str(max_bytes)},
        )

    def presign_download(self, key: str, *, expires: int = 3600) -> str:
        return f"fake://krater-storage/{key}?expires={expires}"

    def head(self, key: str) -> ObjectMeta | None:
        return self._objects.get(key)

    def read_prefix(self, key: str, n: int) -> bytes | None:
        content = self._content.get(key)
        if content is None:
            return None
        return content[:n]

    def read_tail(self, key: str, n: int) -> bytes | None:
        content = self._content.get(key)
        if content is None:
            return None
        return content[-n:] if n else b""

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self._content.pop(key, None)

    def presign_put(self, key: str, *, content_length: int, expires: int = 300) -> str:
        return f"fake://krater-storage/{key}?put&length={content_length}&expires={expires}"

    def list_prefix(self, prefix: str) -> list[ListedObject]:
        return [
            ListedObject(key=k, size_bytes=m.size_bytes)
            for k, m in sorted(self._objects.items())
            if k.startswith(prefix)
        ]

    def create_multipart_upload(self, key: str) -> str:
        upload_id = f"upload-{next(self._upload_ids)}"
        self.multipart_uploads[upload_id] = key
        return upload_id

    def list_multipart_uploads(self, prefix: str) -> list[MultipartUpload]:
        return [
            MultipartUpload(key=key, upload_id=upload_id, initiated=None)
            for upload_id, key in sorted(self.multipart_uploads.items())
            if key.startswith(prefix)
        ]

    def abort_multipart_upload(self, key: str, *, upload_id: str) -> None:
        if self.multipart_uploads.get(upload_id) == key:
            del self.multipart_uploads[upload_id]

    def presign_upload_part(
        self, key: str, *, upload_id: str, part_number: int, content_length: int, expires: int = 300
    ) -> str:
        return (
            f"fake://krater-storage/{key}?uploadId={upload_id}&partNumber={part_number}"
            f"&length={content_length}&expires={expires}"
        )

    def complete_multipart_upload(self, key: str, *, upload_id: str, parts: list[tuple[int, str]]) -> None:
        if self.multipart_uploads.pop(upload_id, None) != key:
            raise ValueError(f"no multipart upload {upload_id!r} for {key!r}")
        self._objects[key] = ObjectMeta(size_bytes=len(parts), content_type=None)

    # -- Test helpers ----------------------------------------------------------------------------------

    def put(self, key: str, *, content_type: str, size_bytes: int, content: bytes = b"") -> None:
        """Simulate a browser having uploaded `key` through the presigned POST above. `content` is the
        object's actual bytes (or just enough of a prefix for a signature check) -- tests that need
        `confirm_screenshot` to accept the upload must pass real magic bytes here; the default (empty)
        deliberately fails any signature check, the same way an empty/garbage upload would for real."""
        self._objects[key] = ObjectMeta(size_bytes=size_bytes, content_type=content_type)
        self._content[key] = content


__all__ = ["FakeObjectStore"]
