"""What a job left in storage, for its team: listing, downloads and the end of its log."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krater.services import compute_files, compute_jobs
from krater.services.compute_jobs import JobSpec
from krater.services.errors import NotAllowed, NotFound
from krater.storage.fake import FakeObjectStore
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB
from tests.web.conftest import actor_for


@pytest.fixture
def project(login_as, approved_project):
    return approved_project(login_as(MEMBER_SUB), login_as(REVIEWER_SUB))


@pytest.fixture
def job(db_session, project, login_as):
    spec = JobSpec(
        name="train",
        image="alpine:3.20",
        command="true",
        gpu_names=("RTX 3060",),
        target_microusd=70_000,
        max_microusd=100_000,
    )
    member = actor_for(login_as(MEMBER_SUB))
    return compute_jobs.submit_job(db_session, member, project=project, spec=spec, cap_cents=500, now=datetime.now(UTC))


def _put(store, job, area, name, content: bytes) -> None:
    key = f"projects/{job.project_id}/jobs/{job.id}/{area}/{name}"
    store.put(key, content_type="", size_bytes=len(content), content=content)


def test_the_team_lists_outputs_checkpoints_and_logs_but_not_the_workspace(job, login_as) -> None:
    store = FakeObjectStore()
    _put(store, job, "outputs", "eval/score.txt", b"0.93\n")
    _put(store, job, "checkpoints", "step-1.pt", b"w")
    _put(store, job, "logs", "attempt-1.log", b"hello\n")
    _put(store, job, "workspace", "notes.txt", b"n")

    files = compute_files.list_files(store, actor_for(login_as(MEMBER_SUB)), job=job)

    assert [(f.area, f.name, f.size_bytes) for f in files] == [
        ("outputs", "eval/score.txt", 5),
        ("checkpoints", "step-1.pt", 1),
        ("logs", "attempt-1.log", 6),
    ]


def test_reviewers_and_outsiders_cannot_read_a_jobs_files(job, login_as) -> None:
    store = FakeObjectStore()
    for sub in (REVIEWER_SUB, OTHER_MEMBER_SUB):
        with pytest.raises(NotAllowed):
            compute_files.list_files(store, actor_for(login_as(sub)), job=job)


def test_a_download_is_only_for_a_file_the_job_has(job, login_as) -> None:
    store = FakeObjectStore()
    _put(store, job, "outputs", "model.bin", b"m")
    member = actor_for(login_as(MEMBER_SUB))

    url = compute_files.download_url(store, member, job=job, area="outputs", name="model.bin")

    assert url.startswith(f"fake://krater-storage/projects/{job.project_id}/jobs/{job.id}/outputs/model.bin")
    assert "expires=900" in url
    for area, name in (("outputs", "../../other/outputs/x"), ("workspace", "notes.txt"), ("outputs", "nope")):
        with pytest.raises(NotFound):
            compute_files.download_url(store, member, job=job, area=area, name=name)


def test_the_log_tail_is_the_latest_attempts_and_starts_at_a_whole_line(job, login_as) -> None:
    store = FakeObjectStore()
    _put(store, job, "logs", "attempt-1.log", b"first attempt\n")
    _put(store, job, "logs", "attempt-2.log", b"line one\nline two\nline three\n")
    member = actor_for(login_as(MEMBER_SUB))

    tail = compute_files.log_tail(store, member, job=job, max_bytes=16)

    assert (tail.attempt, tail.text, tail.truncated) == (2, "line three\n", True)
    assert compute_files.log_tail(store, member, job=job, attempt=1).text == "first attempt\n"
    assert compute_files.log_tail(store, member, job=job, attempt=3) is None


def test_no_log_yet(job, login_as) -> None:
    assert compute_files.log_tail(FakeObjectStore(), actor_for(login_as(MEMBER_SUB)), job=job) is None
