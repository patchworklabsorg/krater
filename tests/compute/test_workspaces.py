"""Box workspaces: a box's saved /workspace kept apart from the box, continued by a later box one at a time, ended
with a save, reported by the runner, browsed, and deleted."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import sqlalchemy as sa

from krater.compute import runner_api
from krater.models import (
    AuditEvent,
    ComputeInstanceState,
    ComputeJobKind,
    ComputeJobState,
    ComputeWorkspace,
)
from krater.services import boxes, compute_jobs, workspaces
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.storage.fake import FakeObjectStore
from tests.compute.test_boxes import _beat, _box, _events, _running_box
from tests.compute.test_scheduler import NOW, _pass
from tests.services.conftest import add_to_team


def _stopped_box(db_session, member, project, client, fake):
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    _beat(db_session, row, 4)
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=4, seconds=30))
    _pass(db_session, client, NOW + timedelta(minutes=5))
    assert job.state is ComputeJobState.STOPPED
    return job, row


# -- Every box has a workspace --------------------------------------------------------------------------------------


def test_a_new_box_starts_a_workspace_named_after_it_at_its_own_files(db_session, member, project) -> None:
    job = _box(db_session, member, project, name="explore")

    workspace = job.workspace
    assert workspace is not None and workspace.name == "explore"
    assert workspace.prefix == f"projects/{project.id}/jobs/{job.id}/workspace/"
    assert workspaces.prefix_for(job) == workspace.prefix
    assert workspace.origin_job_id == job.id and workspace.created_by_id == member.user.id


def test_a_new_workspace_can_be_named(db_session, member, project) -> None:
    job = _box(db_session, member, project, workspace_name="  llm   finetune ")

    assert job.workspace.name == "llm finetune"


def test_a_job_has_no_workspace(db_session, member, project) -> None:
    job = _box(db_session, member, project, kind=ComputeJobKind.JOB, command="true")
    assert job.workspace is None

    with pytest.raises(ValidationFailed) as caught:
        _box(db_session, member, project, kind=ComputeJobKind.JOB, command="true", workspace_name="x")
    assert "workspace" in caught.value.errors


def test_the_scheduler_gives_a_box_from_an_older_release_a_workspace(db_session, member, project, client) -> None:
    job = _box(db_session, member, project)
    old = job.workspace
    job.workspace_id, job.workspace = None, None
    db_session.delete(old)
    db_session.flush()

    _pass(db_session, client)

    db_session.refresh(job)
    assert job.workspace is not None and job.workspace.prefix == workspaces.job_prefix(job)


# -- Continuing one, one box at a time -------------------------------------------------------------------------------


def test_a_later_box_continues_an_ended_boxs_workspace(db_session, member, project, client, fake) -> None:
    first, _ = _running_box(db_session, member, project, client, fake)
    compute_jobs.cancel_job(db_session, member, job=first, now=NOW + timedelta(minutes=3))

    second = _box(db_session, member, project, name="again", workspace_id=first.workspace_id)

    assert second.workspace_id == first.workspace_id
    assert workspaces.prefix_for(second) == f"projects/{project.id}/jobs/{first.id}/workspace/"
    assert "workspace_continued" in _events(db_session, second)
    assert workspaces.holder(db_session, first.workspace).id == second.id


def test_a_workspace_a_running_box_uses_cant_be_continued(db_session, member, project, client, fake) -> None:
    first, _ = _running_box(db_session, member, project, client, fake)

    with pytest.raises(ValidationFailed) as caught:
        _box(db_session, member, project, workspace_id=first.workspace_id, end_holder=True)

    assert "in use by the box" in caught.value.errors["workspace"]
    assert first.state is ComputeJobState.RUNNING


def test_a_queued_box_holds_its_workspace_too(db_session, member, project) -> None:
    first = _box(db_session, member, project)

    with pytest.raises(ValidationFailed):
        _box(db_session, member, project, workspace_id=first.workspace_id)


def test_a_stopped_holder_is_ended_only_when_asked(db_session, member, project, client, fake) -> None:
    first, row = _stopped_box(db_session, member, project, client, fake)

    with pytest.raises(ValidationFailed) as caught:
        _box(db_session, member, project, workspace_id=first.workspace_id)
    assert "Tick the box" in caught.value.errors["workspace"]

    second = _box(db_session, member, project, name="next", workspace_id=first.workspace_id, end_holder=True)

    assert first.state is ComputeJobState.CANCELLED and "'next'" in first.state_reason
    assert second.workspace_id == first.workspace_id
    _pass(db_session, client, NOW + timedelta(minutes=6))
    assert row.state is ComputeInstanceState.DESTROYED


def test_the_whole_team_can_continue_a_workspace_but_no_one_else(
    db_session, member, project, client, fake, make_actor
) -> None:
    from krater.services.actor import GROUP_MEMBER

    first = _box(db_session, member, project)
    compute_jobs.cancel_job(db_session, member, job=first, now=NOW)
    teammate = make_actor(groups=frozenset({GROUP_MEMBER}))
    add_to_team(db_session, project, teammate)
    outsider = make_actor(groups=frozenset({GROUP_MEMBER}))

    assert workspaces.can_see(teammate, project) and not workspaces.can_see(outsider, project)
    with pytest.raises(NotAllowed):
        workspaces.get(db_session, outsider, project, first.workspace_id)
    second = _box(db_session, teammate, project, workspace_id=first.workspace_id)
    assert second.workspace_id == first.workspace_id


def test_another_projects_workspace_cant_be_continued(db_session, member, project, admin, weave) -> None:
    from tests.factories import new_project

    other = new_project(db_session, member, title="Other", write_up="w", budget_requested_cents=100)
    foreign = ComputeWorkspace(
        project_id=other.id, name="theirs", created_by_id=member.user.id, prefix=f"projects/{other.id}/x/"
    )
    db_session.add(foreign)
    db_session.flush()

    with pytest.raises(ValidationFailed) as caught:
        _box(db_session, member, project, workspace_id=foreign.id)
    assert "isn't one of this project's" in caught.value.errors["workspace"]


def test_a_stop_without_a_confirmed_save_is_flagged(db_session, member, project, client, fake) -> None:
    first, row = _stopped_box(db_session, member, project, client, fake)
    assert workspaces.last_stop_saved(first)

    row.last_synced_at = row.stopped_at - timedelta(hours=1)

    assert not workspaces.last_stop_saved(first)


def test_find_takes_an_id_a_prefix_or_a_name(db_session, member, project) -> None:
    one = _box(db_session, member, project, name="one")
    compute_jobs.cancel_job(db_session, member, job=one, now=NOW)

    assert workspaces.find(db_session, project, str(one.workspace_id)) is one.workspace
    assert workspaces.find(db_session, project, str(one.workspace_id)[:8]) is one.workspace
    assert workspaces.find(db_session, project, "one") is one.workspace
    assert workspaces.find(db_session, project, "nope") is None
    _box(db_session, member, project, name="one")
    with pytest.raises(ValidationFailed):
        workspaces.find(db_session, project, "one")


# -- End box: save, then destroy --------------------------------------------------------------------------------------


def test_end_box_saves_then_destroys_and_the_workspace_stays(db_session, member, project, client, fake) -> None:
    job, row = _running_box(db_session, member, project, client, fake)

    compute_jobs.end_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    assert job.end_requested_at is not None and job.state is ComputeJobState.RUNNING
    _pass(db_session, client, NOW + timedelta(minutes=3))
    assert row.stop_requested_at is not None and job.state is ComputeJobState.RUNNING
    assert _beat(db_session, row, 4).sync is True
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=4, seconds=30))
    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert job.state is ComputeJobState.CANCELLED
    assert "/workspace saved" in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED
    assert "ended" in _events(db_session, job)
    assert job.workspace.deleted_at is None


def test_end_box_destroys_at_the_sync_deadline_without_a_save(db_session, member, project, client, fake) -> None:
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.end_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))

    _pass(db_session, client, NOW + timedelta(minutes=3))
    _pass(db_session, client, NOW + timedelta(minutes=20))

    assert job.state is ComputeJobState.CANCELLED and "not confirmed saved" in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED


def test_ending_a_stopped_box_destroys_it_at_once(db_session, member, project, client, fake) -> None:
    job, row = _stopped_box(db_session, member, project, client, fake)

    compute_jobs.end_box(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.CANCELLED
    assert row.state is ComputeInstanceState.DESTROYED


def test_end_now_cuts_an_ending_box_short(db_session, member, project, client, fake) -> None:
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.end_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    _pass(db_session, client, NOW + timedelta(minutes=3))

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=4))
    _pass(db_session, client, NOW + timedelta(minutes=4))

    assert job.state is ComputeJobState.CANCELLED
    assert row.state is ComputeInstanceState.DESTROYED


def test_only_boxes_end(db_session, member, project) -> None:
    job = _box(db_session, member, project, kind=ComputeJobKind.JOB, command="true")

    with pytest.raises(InvalidState):
        compute_jobs.end_box(db_session, member, job=job, now=NOW)


# -- The runner's save reports ----------------------------------------------------------------------------------------


def test_a_timed_save_is_recorded_as_of_its_scan_and_never_confirms_a_stop(
    db_session, member, project, client, fake
) -> None:
    job, row = _running_box(db_session, member, project, client, fake)
    at = NOW + timedelta(minutes=12)

    runner_api.workspace_saved(db_session, row, {"scan_seconds": 90, "files": 2357, "bytes": 4096}, now=at)

    assert row.workspace_saved_at == at - timedelta(seconds=90)
    assert (row.workspace_files, row.workspace_bytes) == (2357, 4096)
    assert row.last_synced_at is None  # only a save Krater asked for confirms a stop
    assert job.workspace.last_saved_at == at - timedelta(seconds=90) and job.workspace.files == 2357
    assert boxes.last_saved(job) == at - timedelta(seconds=90)


def test_a_reported_scan_time_is_clamped(db_session, member, project, client, fake) -> None:
    _, row = _running_box(db_session, member, project, client, fake)
    at = NOW + timedelta(hours=10)

    runner_api.workspace_saved(db_session, row, {"scan_seconds": 10**9}, now=at)
    assert row.workspace_saved_at == at - timedelta(hours=6)
    runner_api.workspace_saved(db_session, row, {"scan_seconds": "nan"}, now=at + timedelta(minutes=1))
    assert row.workspace_saved_at == at + timedelta(minutes=1)


def test_failing_saves_show_and_the_third_tells_the_team_once(db_session, member, project, client, fake) -> None:
    job, row = _running_box(db_session, member, project, client, fake)

    for minute in range(10, 50, 10):
        runner_api.workspace_save_failed(
            db_session, row, {"error": "upload refused: storage quota full"}, now=NOW + timedelta(minutes=minute)
        )

    trouble = boxes.save_trouble(row)
    assert trouble is not None and trouble.failures == 4 and "quota" in trouble.error
    notices = db_session.scalars(
        sa.select(AuditEvent).where(AuditEvent.action == runner_api.AUDIT_BOX_SAVE_FAILING)
    ).all()
    assert len(notices) == 1

    runner_api.workspace_saved(db_session, row, {"scan_seconds": 1}, now=NOW + timedelta(minutes=60))
    assert boxes.save_trouble(row) is None
    assert "workspace_saves_ok" in _events(db_session, job)


def test_a_finished_box_cant_change_its_workspace(db_session, member, project, client, fake) -> None:
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=3))

    with pytest.raises(NotAllowed):
        runner_api.begin_upload(
            db_session,
            FakeObjectStore(),
            row,
            "workspace",
            "a.txt",
            1,
            quota_bytes=10**9,
            now=NOW + timedelta(minutes=3),
        )


# -- Its files, and deleting it ---------------------------------------------------------------------------------------


def _manifest(store, workspace, entries) -> None:
    raw = json.dumps({"version": 1, "entries": entries}).encode()
    store.put(workspace.prefix + workspaces.MANIFEST, content_type="", size_bytes=len(raw), content=raw)


def test_the_listing_follows_the_manifest(db_session, member, project) -> None:
    job = _box(db_session, member, project)
    store = FakeObjectStore()
    workspace = job.workspace
    store.put(workspace.prefix + "train.py", content_type="", size_bytes=10)
    store.put(workspace.prefix + ".krater-files/" + "ab" * 20, content_type="", size_bytes=7)
    store.put(workspace.prefix + "deleted-on-the-box.txt", content_type="", size_bytes=3)
    _manifest(
        store,
        workspace,
        [
            {"path": "venv", "type": "dir", "mode": 2147484141},
            {"path": "venv/bin/python", "type": "link", "target": "/usr/bin/python3"},
            {"path": "train.py", "type": "file", "mode": 493, "mtime": 1_763_000_000_000_000_000},
            {"path": "notes/a\nb.txt", "type": "file", "stored": ".krater-files/" + "ab" * 20},
            {"path": "never-uploaded.bin", "type": "file"},
        ],
    )

    shown = workspaces.listing(store, workspace)

    assert shown.has_manifest
    assert [(e.path, e.kind) for e in shown.entries] == [
        ("notes/a\nb.txt", "file"),
        ("train.py", "file"),
        ("venv", "dir"),
        ("venv/bin/python", "link"),
    ]
    assert shown.entries[1].mode == 493 and shown.entries[1].size_bytes == 10
    url = workspaces.download_url(store, workspace, "notes/a\nb.txt")
    assert url.startswith("fake://krater-storage/" + workspace.prefix + ".krater-files/")
    with pytest.raises(NotFound):
        workspaces.download_url(store, workspace, "deleted-on-the-box.txt")
    with pytest.raises(NotFound):
        workspaces.download_url(store, workspace, "../../inputs/secret.csv")


def test_a_workspace_without_a_manifest_lists_its_files(db_session, member, project) -> None:
    job = _box(db_session, member, project)
    store = FakeObjectStore()
    store.put(job.workspace.prefix + "a.txt", content_type="", size_bytes=1)

    shown = workspaces.listing(store, job.workspace)

    assert not shown.has_manifest and [e.path for e in shown.entries] == ["a.txt"]


def test_deleting_a_workspace_removes_its_files_and_is_audited(db_session, member, project) -> None:
    job = _box(db_session, member, project)
    store = FakeObjectStore()
    store.put(job.workspace.prefix + "a.txt", content_type="", size_bytes=5)
    store.put(f"projects/{project.id}/inputs/keep.csv", content_type="", size_bytes=5)

    with pytest.raises(InvalidState):
        workspaces.delete(db_session, store, member, workspace=job.workspace, now=NOW)  # its box holds it
    compute_jobs.cancel_job(db_session, member, job=job, now=NOW)
    count = workspaces.delete(db_session, store, member, workspace=job.workspace, now=NOW)

    assert count == 1
    assert store.list_prefix(job.workspace.prefix) == []
    assert len(store.list_prefix(f"projects/{project.id}/inputs/")) == 1
    assert job.workspace.deleted_at == NOW and job.workspace.deleted_by_id == member.user.id
    assert workspaces.list_workspaces(db_session, project) == []
    audit = db_session.scalars(
        sa.select(AuditEvent).where(AuditEvent.action == workspaces.AUDIT_WORKSPACE_DELETED)
    ).one()
    assert audit.payload["objects"] == 1
    with pytest.raises(ValidationFailed):
        _box(db_session, member, project, workspace_id=job.workspace_id)


def test_the_project_files_summary_sorts_storage_by_kind(db_session, member, project) -> None:
    from krater.services import project_files

    box = _box(db_session, member, project, name="box")
    job = _box(db_session, member, project, name="job", kind=ComputeJobKind.JOB, command="true")
    store = FakeObjectStore()
    store.put(box.workspace.prefix + "a.txt", content_type="", size_bytes=100)
    store.put(f"projects/{project.id}/jobs/{job.id}/outputs/r.csv", content_type="", size_bytes=10)
    store.put(f"projects/{project.id}/jobs/{job.id}/logs/attempt-1.log", content_type="", size_bytes=1)
    store.put(f"projects/{project.id}/inputs/d.csv", content_type="", size_bytes=1000)

    shown = project_files.summary(db_session, store, member, project)

    assert (shown.total.files, shown.total.bytes) == (4, 1111)
    assert shown.inputs.bytes == 1000
    assert [(w.workspace.name, w.stored.bytes, w.holder.id) for w in shown.workspaces] == [("box", 100, box.id)]
    assert [(j.job.name, {a: t.bytes for a, t in j.areas.items()}) for j in shown.jobs] == [
        ("job", {"outputs": 10, "logs": 1})
    ]
    assert shown.other.files == 0


def test_only_the_team_and_admins_see_the_projects_files(db_session, member, project, admin, make_actor) -> None:
    from krater.services import project_files
    from krater.services.actor import GROUP_MEMBER

    outsider = make_actor(groups=frozenset({GROUP_MEMBER}))

    assert project_files.summary(db_session, FakeObjectStore(), admin, project).total.files == 0
    with pytest.raises(NotAllowed):
        project_files.summary(db_session, FakeObjectStore(), outsider, project)
