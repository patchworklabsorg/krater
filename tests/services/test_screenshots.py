"""Screenshot upload rules: submitter-only, completion-draft-only, the 6-screenshot limit, allowed
content types/sizes, and confirm re-validating against storage rather than trusting the client."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.models import ProjectStatus, ReviewDecision, ReviewSource
from krater.services import projects, screenshots
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.storage.fake import FakeObjectStore


@pytest.fixture
def store() -> FakeObjectStore:
    return FakeObjectStore()


#: Real magic-byte headers for each allowed type, padded out past `SIGNATURE_CHECK_BYTES` so tests can
#: use these directly as a confirmed upload's `content`.
_VALID_SIGNATURES: dict[str, bytes] = {
    "image/png": bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)) + b"\x00" * 8,
    "image/jpeg": bytes((0xFF, 0xD8, 0xFF)) + b"\x00" * 13,
    "image/webp": b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4,
}


def _completion_draft(db_session: Session, member: Actor, reviewer: Actor):
    """An approved project with a fresh, unsubmitted completion draft."""
    project = projects.create_project(
        db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=100_000
    )
    project = projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED

    draft = projects.start_completion(db_session, member, project=project)
    return project, draft


def _confirm_upload(
    db_session: Session,
    member: Actor,
    *,
    project,
    store: FakeObjectStore,
    content_type: str = "image/png",
    size_bytes: int = 1024,
) -> str:
    """Presign, simulate the browser's upload (with a real signature for `content_type`), then confirm.
    Returns the confirmed key."""
    upload = screenshots.presign_screenshot(db_session, member, project=project, content_type=content_type, store=store)
    store.put(upload.key, content_type=content_type, size_bytes=size_bytes, content=_VALID_SIGNATURES[content_type])
    screenshots.confirm_screenshot(db_session, member, project=project, key=upload.key, store=store)
    return upload.key


# --------------------------------------------------------------------------------------------------
# Authorization and state
# --------------------------------------------------------------------------------------------------


def test_non_submitter_cannot_presign(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, _draft = _completion_draft(db_session, member, reviewer)

    with pytest.raises(NotAllowed):
        screenshots.presign_screenshot(db_session, reviewer, project=project, content_type="image/png", store=store)


def test_non_submitter_cannot_confirm(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)
    upload = screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)
    store.put(upload.key, content_type="image/png", size_bytes=100)

    with pytest.raises(NotAllowed):
        screenshots.confirm_screenshot(db_session, reviewer, project=project, key=upload.key, store=store)


def test_non_submitter_cannot_remove(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, _draft = _completion_draft(db_session, member, reviewer)
    key = _confirm_upload(db_session, member, project=project, store=store)

    with pytest.raises(NotAllowed):
        screenshots.remove_screenshot(db_session, reviewer, project=project, key=key, store=store)


def test_cannot_presign_on_a_non_completion_draft(db_session: Session, member: Actor, store: FakeObjectStore) -> None:
    project = projects.create_project(db_session, member, title="", write_up="", budget_requested_cents=0)
    assert project.current_revision.kind.value == "proposal"

    with pytest.raises(InvalidState):
        screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)


def test_cannot_presign_once_completion_is_submitted(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)
    projects.update_draft(db_session, member, project=project, write_up="Done.")
    projects.submit_completion(db_session, member, project=project)

    with pytest.raises(InvalidState):
        screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)


# --------------------------------------------------------------------------------------------------
# Limits and content validation
# --------------------------------------------------------------------------------------------------


def test_at_most_six_screenshots(db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore) -> None:
    project, _draft = _completion_draft(db_session, member, reviewer)
    for _ in range(screenshots.MAX_SCREENSHOTS):
        _confirm_upload(db_session, member, project=project, store=store)

    with pytest.raises(ValidationFailed):
        screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)


def test_confirm_also_enforces_the_limit(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    """Two presigns can be issued before either confirms; confirm re-checks the limit, not just presign."""
    project, draft = _completion_draft(db_session, member, reviewer)
    for _ in range(screenshots.MAX_SCREENSHOTS - 1):
        _confirm_upload(db_session, member, project=project, store=store)

    upload_a = screenshots.presign_screenshot(
        db_session, member, project=project, content_type="image/png", store=store
    )
    upload_b = screenshots.presign_screenshot(
        db_session, member, project=project, content_type="image/png", store=store
    )
    store.put(upload_a.key, content_type="image/png", size_bytes=100, content=_VALID_SIGNATURES["image/png"])
    store.put(upload_b.key, content_type="image/png", size_bytes=100, content=_VALID_SIGNATURES["image/png"])

    screenshots.confirm_screenshot(db_session, member, project=project, key=upload_a.key, store=store)
    assert len(draft.screenshot_keys) == screenshots.MAX_SCREENSHOTS

    with pytest.raises(ValidationFailed):
        screenshots.confirm_screenshot(db_session, member, project=project, key=upload_b.key, store=store)


def test_presign_rejects_a_disallowed_content_type(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, _draft = _completion_draft(db_session, member, reviewer)

    with pytest.raises(ValidationFailed):
        screenshots.presign_screenshot(db_session, member, project=project, content_type="image/gif", store=store)


def test_confirm_rejects_a_missing_object(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)

    with pytest.raises(ValidationFailed):
        screenshots.confirm_screenshot(
            db_session, member, project=project, key=f"projects/{project.id}/{draft.id}/never-uploaded.png", store=store
        )


def test_confirm_rejects_an_oversized_object(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    """The presigned policy is storage's own enforcement; confirm re-checks server-side too, since a
    storage backend that doesn't honor every policy condition must not let an oversized object in."""
    project, draft = _completion_draft(db_session, member, reviewer)
    upload = screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)
    store.put(upload.key, content_type="image/png", size_bytes=screenshots.MAX_SCREENSHOT_BYTES + 1)

    with pytest.raises(ValidationFailed):
        screenshots.confirm_screenshot(db_session, member, project=project, key=upload.key, store=store)

    assert upload.key not in draft.screenshot_keys
    assert store.head(upload.key) is None  # best-effort deleted


def test_confirm_rejects_a_mismatched_content_type(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    """Defense in depth: `head`'s reported content type is checked again at confirm time, independent
    of whatever the presigned policy required at upload time (a real S3-compatible backend does enforce
    that condition -- confirmed against SeaweedFS in tests/live/test_storage_live.py -- but this check
    doesn't rely on it)."""
    project, draft = _completion_draft(db_session, member, reviewer)
    upload = screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)
    store.put(upload.key, content_type="text/plain", size_bytes=10)

    with pytest.raises(ValidationFailed):
        screenshots.confirm_screenshot(db_session, member, project=project, key=upload.key, store=store)

    assert upload.key not in draft.screenshot_keys


def test_confirm_rejects_bytes_that_dont_match_the_declared_signature(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    """A client can legitimately sign a presigned POST for `image/png` and then upload arbitrary bytes
    under that label (the stored Content-Type comes from the signed form field, not the bytes -- see
    docs/dev/storage.md); the magic-byte check is what actually catches that."""
    project, draft = _completion_draft(db_session, member, reviewer)
    upload = screenshots.presign_screenshot(db_session, member, project=project, content_type="image/png", store=store)
    store.put(upload.key, content_type="image/png", size_bytes=100, content=b"not actually a png file at all!")

    with pytest.raises(ValidationFailed):
        screenshots.confirm_screenshot(db_session, member, project=project, key=upload.key, store=store)

    assert upload.key not in draft.screenshot_keys
    assert store.head(upload.key) is None  # best-effort deleted


@pytest.mark.parametrize("content_type", sorted(screenshots.ALLOWED_CONTENT_TYPES))
def test_confirm_accepts_a_valid_signature_for_every_allowed_type(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore, content_type: str
) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)

    key = _confirm_upload(db_session, member, project=project, store=store, content_type=content_type)

    assert key in draft.screenshot_keys


def test_confirm_rejects_a_key_outside_this_draft(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, _draft = _completion_draft(db_session, member, reviewer)
    store.put("projects/someone-elses/rev/x.png", content_type="image/png", size_bytes=10)

    with pytest.raises(NotAllowed):
        screenshots.confirm_screenshot(
            db_session, member, project=project, key="projects/someone-elses/rev/x.png", store=store
        )


def test_confirm_is_idempotent(db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)
    key = _confirm_upload(db_session, member, project=project, store=store)

    screenshots.confirm_screenshot(db_session, member, project=project, key=key, store=store)
    assert draft.screenshot_keys.count(key) == 1


# --------------------------------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------------------------------


def test_remove_deletes_from_the_draft_and_storage(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)
    key = _confirm_upload(db_session, member, project=project, store=store)
    assert store.head(key) is not None

    screenshots.remove_screenshot(db_session, member, project=project, key=key, store=store)

    assert key not in draft.screenshot_keys
    assert store.head(key) is None


def test_remove_is_best_effort_against_storage_failures(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    """Storage being unreachable shouldn't block dropping the key from the draft."""
    from krater.storage.errors import StorageUnavailableError

    project, draft = _completion_draft(db_session, member, reviewer)
    key = _confirm_upload(db_session, member, project=project, store=store)

    def _broken_delete(_key: str) -> None:
        raise StorageUnavailableError("storage is down")

    store.delete = _broken_delete  # type: ignore[method-assign]

    screenshots.remove_screenshot(db_session, member, project=project, key=key, store=store)
    assert key not in draft.screenshot_keys


def test_cannot_remove_a_key_not_on_the_draft(
    db_session: Session, member: Actor, reviewer: Actor, store: FakeObjectStore
) -> None:
    project, draft = _completion_draft(db_session, member, reviewer)

    with pytest.raises(InvalidState):
        screenshots.remove_screenshot(
            db_session, member, project=project, key=f"projects/{project.id}/{draft.id}/nope.png", store=store
        )
