"""Image uploads on a completion draft: presign, confirm, remove.

A completion has two kinds of image (`role`). The **primary** image (`PRIMARY`, at most one, in
`ProjectRevision.screenshot_keys`) is the one the gallery shows on its list and at the top of the page: a new
upload replaces it. **Inline** images (`INLINE`, up to `MAX_INLINE_IMAGES`, in `ProjectRevision.image_keys`)
are the ones the final write-up shows, through Markdown that names them by `image_path`. Krater hosts both: the
renderer (`krater.web.textfmt.markdown`) shows only images at `HOSTED_IMAGE_PATH`, and that route redirects to a
short-lived storage URL once it has checked the viewer may see the revision.

See `docs/SPEC.md` ("Completion flow & public gallery": "Uploads use presigned URLs, and only objects
from approved completion revisions are served publicly"). The three-step flow this module implements:

1. `presign_screenshot` -- the submitter asks for somewhere to upload. This picks the key (never the
   client) and returns a presigned `POST` policy that pins the object's size and content type into the
   signature, so storage itself rejects an oversized or mislabeled upload.
2. The browser uploads directly to storage, bypassing Krater's own process entirely.
3. `confirm_screenshot` -- the submitter tells Krater the upload finished. This is the only step that
   actually changes the revision's keys, and it re-checks the object via `ObjectStore.head`
   and `ObjectStore.read_prefix` before trusting it: the presigned policy is *storage's* enforcement,
   not Krater's, so a client that skips step 1 (or a storage backend that doesn't enforce every policy
   condition -- see `docs/dev/storage.md`) must not be able to sneak an oversized, wrong-type, or
   not-actually-that-type object into the gallery. The signature check catches the specific gap
   `docs/dev/storage.md` documents: a real S3-compatible backend takes the stored `Content-Type` from
   the presigned POST's form field, not from the uploaded bytes themselves, so a client can legitimately
   sign for `image/png` and then upload anything under that label.

No image is ever *decoded* (no Pillow) -- only its first few bytes are checked against the declared
type's magic number (PNG/JPEG/WebP each start with a fixed signature). That's an acceptable gap for now
because: uploads require a signed-in member on their own completion draft, not an anonymous/public
endpoint; a file whose signature matches but whose body is otherwise malformed is only ever rendered
back as an `<img src>`, which just fails to display rather than executing anything; and decoding
untrusted image bytes server-side is its own attack surface (image-parser CVEs) not worth adding for
this. If images ever accept broader or more adversarial input, add a decode-and-reencode step
before confirming.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from krater.models import Project, ProjectRevision, RevisionKind
from krater.services import teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.storage.client import ObjectStore
from krater.storage.errors import StorageError
from krater.storage.types import PresignedPost

#: PNG / JPEG / WebP only, per docs/SPEC.md's image upload scope. Maps content type -> the
#: extension used in the generated key (cosmetic only; the type actually stored comes from `head`).
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

#: The two kinds of image a completion has (module docstring).
PRIMARY = "primary"
INLINE = "inline"
IMAGE_ROLES = (PRIMARY, INLINE)

MAX_INLINE_IMAGES = 20
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_IMAGE_FILE = rf"{_UUID}\.(?:png|jpg|webp)"
#: Where Krater serves an inline image: `/projects/<project_id>/images/<name>`, `<name>` being the last part of
#: its key. Matched whole (`fullmatch`), so no query, fragment, `..` or other site gets through.
HOSTED_IMAGE_PATH = re.compile(rf"/projects/{_UUID}/images/{_IMAGE_FILE}")

#: How many leading bytes `confirm_screenshot` reads to check a file's magic-byte signature. 16 is more
#: than any of the three signatures below need (WebP's is the longest, at 12: `RIFF` + a 4-byte size
#: field + `WEBP`), with a little headroom.
SIGNATURE_CHECK_BYTES = 16

#: PNG and JPEG signatures are a fixed byte prefix. WebP's isn't quite (bytes 4-7 are a little-endian
#: file size, not part of the signature), so it gets its own check in `_matches_signature` below.
_FIXED_SIGNATURES: dict[str, bytes] = {
    "image/png": bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)),
    "image/jpeg": bytes((0xFF, 0xD8, 0xFF)),
}


def _matches_signature(content_type: str, prefix: bytes) -> bool:
    """Whether `prefix` (the object's leading bytes) starts with `content_type`'s magic number."""
    if content_type == "image/webp":
        return prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    signature = _FIXED_SIGNATURES.get(content_type)
    return signature is not None and prefix.startswith(signature)


@dataclass(frozen=True)
class ScreenshotUpload:
    """What `presign_screenshot` hands back: the server-chosen key, and the presigned POST to upload to."""

    key: str
    post: PresignedPost


def _require_completion_draft(actor: Actor, project: Project) -> ProjectRevision:
    """The project's current, unsubmitted `completion` revision, or raise.

    Team only. Shared by all three operations: images only ever belong to the *draft in progress*, never a
    submitted/decided revision (which is immutable review history) or another kind of revision.
    """
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may manage its images.")

    draft = project.current_revision
    if draft is None or draft.submitted_at is not None or draft.kind is not RevisionKind.COMPLETION:
        raise InvalidState("Images can only be added to a completion draft.")
    return draft


def _key_prefix(project: Project, draft: ProjectRevision) -> str:
    return f"projects/{project.id}/{draft.id}/"


def _require_role(role: str) -> None:
    if role not in IMAGE_ROLES:
        raise ValidationFailed({"screenshot": "Unknown kind of image."})


def _inline_limit_error() -> ValidationFailed:
    return ValidationFailed({"screenshot": f"At most {MAX_INLINE_IMAGES} images can go in the write-up."})


def image_name(key: str) -> str:
    """The part of `key` an image's URL names: its file name (`<uuid>.<ext>`)."""
    return key.rsplit("/", 1)[-1]


def image_path(project_id: uuid.UUID, key: str) -> str:
    """Where Krater serves the inline image at `key`, for the write-up's Markdown to name."""
    return f"/projects/{project_id}/images/{image_name(key)}"


def find_image_key(revision: ProjectRevision | None, name: str) -> str | None:
    """The key of `revision`'s inline image called `name`, or `None` if it has none by that name."""
    if revision is None:
        return None
    return next((key for key in revision.image_keys if image_name(key) == name), None)


def presign_screenshot(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    content_type: str,
    store: ObjectStore,
    role: str = PRIMARY,
) -> ScreenshotUpload:
    """Issue a presigned upload for a new image (`role`) on the project's completion draft.

    Validates `content_type` and, for an inline image, the count (`ValidationFailed` otherwise -- the same
    limit is re-checked in `confirm_screenshot`, since two presigns can be issued before either is
    confirmed). A primary image has no count: confirming one replaces the last. The key is always
    server-generated (`projects/<project_id>/<revision_id>/<uuid>.<ext>`), never taken from the caller.
    """
    draft = _require_completion_draft(actor, project)
    _require_role(role)

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationFailed({"screenshot": "Only PNG, JPEG or WebP images are allowed."})
    if role == INLINE and len(draft.image_keys) >= MAX_INLINE_IMAGES:
        raise _inline_limit_error()

    extension = ALLOWED_CONTENT_TYPES[content_type]
    key = f"{_key_prefix(project, draft)}{uuid.uuid4()}.{extension}"
    post = store.presign_upload(key, content_type=content_type, max_bytes=MAX_SCREENSHOT_BYTES)
    return ScreenshotUpload(key=key, post=post)


def confirm_screenshot(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    key: str,
    store: ObjectStore,
    role: str = PRIMARY,
) -> ProjectRevision:
    """Confirm an image finished uploading to `key`: it becomes the draft's primary image (the old one is
    best-effort deleted), or joins its inline images.

    Re-validates everything server-side rather than trusting the browser's earlier presign request:
    `key` must be one this module could have issued for this project's current draft (never an arbitrary,
    client-chosen key), the inline limit must still hold, the object must actually exist in storage with an
    allowed content type and size at or under `MAX_SCREENSHOT_BYTES`, and its first `SIGNATURE_CHECK_BYTES`
    bytes must match that content type's magic number (`ValidationFailed` if any of that doesn't hold,
    including no object at all). A rejected object is best-effort deleted from storage so it doesn't linger.
    """
    draft = _require_completion_draft(actor, project)
    _require_role(role)

    if not re.fullmatch(rf"{re.escape(_key_prefix(project, draft))}{_IMAGE_FILE}", key):
        raise NotAllowed("That upload doesn't belong to this project's draft.")
    if key in draft.screenshot_keys or key in draft.image_keys:
        return draft  # already confirmed; idempotent
    if role == INLINE and len(draft.image_keys) >= MAX_INLINE_IMAGES:
        raise _inline_limit_error()

    meta = store.head(key)
    invalid = meta is None or meta.content_type not in ALLOWED_CONTENT_TYPES or meta.size_bytes > MAX_SCREENSHOT_BYTES
    if not invalid:
        prefix = store.read_prefix(key, SIGNATURE_CHECK_BYTES) or b""
        invalid = not _matches_signature(meta.content_type, prefix)

    if invalid:
        # Best-effort: the object is orphaned but harmless, and never gets linked to the draft either way.
        with contextlib.suppress(StorageError):
            store.delete(key)
        if meta is None:
            raise ValidationFailed({"screenshot": "Upload not found. Try again."})
        raise ValidationFailed({"screenshot": "That upload isn't a valid PNG, JPEG or WebP under 5 MB."})

    if role == INLINE:
        draft.image_keys = [*draft.image_keys, key]
        session.flush()
        return draft

    replaced = list(draft.screenshot_keys)
    draft.screenshot_keys = [key]
    session.flush()
    # Best-effort, like `remove_screenshot`: the draft's own list is the source of truth.
    for old_key in replaced:
        with contextlib.suppress(StorageError):
            store.delete(old_key)
    return draft


def remove_screenshot(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    key: str,
    store: ObjectStore,
) -> ProjectRevision:
    """Remove `key` from the draft's images (primary or inline), and best-effort delete it from storage.

    Raises `InvalidState` if `key` isn't currently one of the draft's images (covers both a typo
    and a double-submit of the remove form). The write-up may still name a removed inline image; it then
    shows as a broken image until the team takes it out of the text.
    """
    draft = _require_completion_draft(actor, project)

    if key in draft.screenshot_keys:
        draft.screenshot_keys = [existing for existing in draft.screenshot_keys if existing != key]
    elif key in draft.image_keys:
        draft.image_keys = [existing for existing in draft.image_keys if existing != key]
    else:
        raise InvalidState("That image isn't on this draft.")
    session.flush()

    # Best-effort, per docs/SPEC.md/module docstring: the draft's own list is the source of truth.
    with contextlib.suppress(StorageError):
        store.delete(key)

    return draft


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "HOSTED_IMAGE_PATH",
    "IMAGE_ROLES",
    "INLINE",
    "MAX_INLINE_IMAGES",
    "MAX_SCREENSHOT_BYTES",
    "PRIMARY",
    "SIGNATURE_CHECK_BYTES",
    "ScreenshotUpload",
    "confirm_screenshot",
    "find_image_key",
    "image_name",
    "image_path",
    "presign_screenshot",
    "remove_screenshot",
]
