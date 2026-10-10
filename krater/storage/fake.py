"""`FakeObjectStore`: an in-memory `ObjectStore` for `KRATER_S3_MODE=fake` (development and tests).

Makes no network calls. Keeps just enough bookkeeping (size + content type + a byte prefix per key) for
`krater.services.screenshots` to exercise the real confirm/head/read_prefix/delete flow, including its
magic-byte signature check. A test that wants to simulate "the browser successfully uploaded through the
presigned POST" calls `put` directly, mirroring how `FakeSkyPilotClient.add_cluster` simulates a fact a
real server would otherwise report.
"""

from __future__ import annotations

from krater.storage.types import ObjectMeta, PresignedPost


class FakeObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, ObjectMeta] = {}
        self._content: dict[str, bytes] = {}

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

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self._content.pop(key, None)

    # -- Test helpers ----------------------------------------------------------------------------------

    def put(self, key: str, *, content_type: str, size_bytes: int, content: bytes = b"") -> None:
        """Simulate a browser having uploaded `key` through the presigned POST above. `content` is the
        object's actual bytes (or just enough of a prefix for a signature check) -- tests that need
        `confirm_screenshot` to accept the upload must pass real magic bytes here; the default (empty)
        deliberately fails any signature check, the same way an empty/garbage upload would for real."""
        self._objects[key] = ObjectMeta(size_bytes=size_bytes, content_type=content_type)
        self._content[key] = content


__all__ = ["FakeObjectStore"]
