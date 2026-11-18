"""Workspaces on the web and in the CLI's API: the project's Files page, a workspace's page, downloads, deleting, the
box form's workspace choice, End box, and the JSON the `krater` command reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeInstance, ComputeInstanceState, ComputeJob, ComputeJobKind, ComputeJobState
from krater.services import compute_jobs, workspaces
from krater.services.compute_jobs import JobSpec
from krater.storage.fake import FakeObjectStore
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import actor_for
from tests.web.test_compute_pages import FORM


@pytest.fixture(autouse=True)
def compute_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")


@pytest.fixture
def project(login_as, approved_project):
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    return approved_project(member, reviewer)


@pytest.fixture
def store(monkeypatch) -> FakeObjectStore:
    fake = FakeObjectStore()
    monkeypatch.setattr("krater.web.routers.compute.get_compute_store", lambda: fake)
    monkeypatch.setattr("krater.web.routers.api.get_compute_store", lambda: fake)
    return fake


def _box(db_session, project, member, **overrides) -> ComputeJob:
    spec = {
        "name": "explore",
        "image": "ubuntu:24.04",
        "kind": ComputeJobKind.BOX,
        "gpu_names": ("RTX 3060",),
        "target_microusd": 70_000,
        "max_microusd": 100_000,
        **overrides,
    }
    return compute_jobs.submit_job(
        db_session,
        actor_for(member),
        project=project,
        spec=JobSpec(**spec),
        cap_cents=500,
        now=datetime.now(UTC),
        weave_client=StubWeaveClient(),
    )


def _saved(store: FakeObjectStore, job: ComputeJob) -> None:
    prefix = job.workspace.prefix
    store.put(prefix + "train.py", content_type="", size_bytes=12, content=b"print('hi')\n")
    store.put(prefix + "data/x.csv", content_type="", size_bytes=4, content=b"1,2\n")
    manifest = json.dumps(
        {
            "version": 1,
            "entries": [
                {"path": "data", "type": "dir", "mode": 493},
                {"path": "data/x.csv", "type": "file", "mode": 420, "mtime": 1_763_000_000_000_000_000},
                {"path": "train.py", "type": "file", "mode": 493},
                {"path": "py", "type": "link", "target": "/usr/bin/python3"},
            ],
        }
    ).encode()
    store.put(prefix + workspaces.MANIFEST, content_type="", size_bytes=len(manifest), content=manifest)


def _ended(db_session, job: ComputeJob) -> None:
    job.state = ComputeJobState.CANCELLED
    db_session.flush()


# -- Pages ------------------------------------------------------------------------------------------------------------


def test_the_files_page_shows_the_quota_and_each_workspace(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _saved(store, job)

    page = client.get(f"/projects/{project.id}/files")

    assert page.status_code == 200
    assert "of 50.0 GB" in page.text or "of 50 GB" in page.text
    assert f"/projects/{project.id}/files/workspaces/{job.workspace_id}" in page.text
    assert "explore" in page.text
    assert f"/projects/{project.id}/files" in client.get(f"/projects/{project.id}/compute").text


def test_only_the_team_and_admins_see_stored_files(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    base = f"/projects/{project.id}/files"

    login_as(ADMIN_SUB)
    assert client.get(base).status_code == 200
    assert client.get(f"{base}/workspaces/{job.workspace_id}").status_code == 200
    login_as(REVIEWER_SUB)
    assert client.get(base).status_code == 403
    assert client.get(f"{base}/workspaces/{job.workspace_id}").status_code == 403
    login_as(OTHER_MEMBER_SUB)
    assert client.get(base).status_code == 404
    assert client.get(f"{base}/workspaces/{job.workspace_id}/download/train.py").status_code == 404


def test_a_workspace_page_browses_folders_and_downloads_files(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _saved(store, job)
    base = f"/projects/{project.id}/files/workspaces/{job.workspace_id}"

    top = client.get(base).text
    folder = client.get(f"{base}?path=data").text
    download = client.get(f"{base}/download/data/x.csv", follow_redirects=False)
    missing = client.get(f"{base}/download/../inputs/x", follow_redirects=False)

    assert "data/</a>" in top and "train.py" in top and "symlink to <code>/usr/bin/python3</code>" in top
    assert "x.csv" in folder and "train.py" not in folder
    assert download.status_code == 303
    assert download.headers["location"].startswith(f"fake://krater-storage/{job.workspace.prefix}data/x.csv")
    assert missing.status_code == 404


def test_a_workspace_in_use_cant_be_deleted_and_an_unused_one_can(
    client: TestClient, login_as, project, db_session, store
):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _saved(store, job)
    db_session.commit()  # a refused delete rolls its request back, which mustn't take the test's rows with it
    base = f"/projects/{project.id}/files/workspaces/{job.workspace_id}"
    page = client.get(base)
    assert "End that box first" in page.text

    client.post(f"{base}/delete", data={"csrf_token": get_csrf_token(page.text), "confirm": "on"})
    assert store.list_prefix(job.workspace.prefix) != []

    _ended(db_session, job)
    page = client.get(base)
    unconfirmed = client.post(f"{base}/delete", data={"csrf_token": get_csrf_token(page.text)}, follow_redirects=False)
    assert store.list_prefix(job.workspace.prefix) != [] and unconfirmed.status_code == 303
    done = client.post(
        f"{base}/delete", data={"csrf_token": get_csrf_token(page.text), "confirm": "on"}, follow_redirects=False
    )

    assert done.status_code == 303 and done.headers["location"] == f"/projects/{project.id}/files"
    assert store.list_prefix(job.workspace.prefix) == []
    assert client.get(base).status_code == 404


def test_deleting_needs_the_csrf_token(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _ended(db_session, job)

    response = client.post(f"/projects/{project.id}/files/workspaces/{job.workspace_id}/delete", data={"confirm": "on"})

    assert response.status_code in (400, 403)
    assert job.workspace.deleted_at is None


def test_the_box_form_offers_the_projects_workspaces(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    first = _box(db_session, project, member, name="first")
    _ended(db_session, first)

    page = client.get(f"/projects/{project.id}/compute/new")
    assert f'value="{first.workspace_id}"' in page.text
    response = client.post(
        f"/projects/{project.id}/compute",
        data={
            **FORM,
            "kind": "box",
            "command": "",
            "image": "ubuntu:24.04",
            "name": "second",
            "workspace": str(first.workspace_id),
            "csrf_token": get_csrf_token(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    second = db_session.scalars(sa.select(ComputeJob).where(ComputeJob.name == "second")).one()
    assert second.workspace_id == first.workspace_id
    again = client.get(f"/projects/{project.id}/compute/new?from={second.id}").text
    assert f'value="{first.workspace_id}" checked' in again


def test_continuing_a_workspace_in_use_is_a_form_error(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    first = _box(db_session, project, member, name="first")
    page = client.get(f"/projects/{project.id}/compute/new")

    response = client.post(
        f"/projects/{project.id}/compute",
        data={
            **FORM,
            "kind": "box",
            "command": "",
            "name": "second",
            "workspace": str(first.workspace_id),
            "csrf_token": get_csrf_token(page.text),
        },
    )

    assert response.status_code == 422
    assert "in use by the box" in response.text


def _running(db_session, job: ComputeJob, **row) -> ComputeInstance:
    job.state = ComputeJobState.RUNNING
    instance = ComputeInstance(
        job_id=job.id,
        attempt=1,
        label="krater-job-x-1",
        provider_instance_id=123,
        state=ComputeInstanceState.RUNNING,
        provider_offer_id="1",
        gpu_name="RTX 3060",
        interruptible=True,
        gpu_microusd=55_000,
        disk_microusd=8_767,
        runner_starts=1,
        **row,
    )
    db_session.add(instance)
    db_session.flush()
    db_session.refresh(job)
    return instance


def test_a_running_box_ends_after_a_save_from_its_page(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _running(db_session, job)

    page = client.get(f"/projects/{project.id}/compute/{job.id}")
    assert "End box" in page.text and "End now" in page.text
    client.post(f"/projects/{project.id}/compute/{job.id}/end", data={"csrf_token": get_csrf_token(page.text)})

    db_session.refresh(job)
    assert job.end_requested_at is not None and job.state is ComputeJobState.RUNNING
    assert "Ending: the box is saving" in " ".join(client.get(f"/projects/{project.id}/compute/{job.id}").text.split())


def test_a_box_whose_saves_fail_says_why(client: TestClient, login_as, project, db_session, store):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _running(
        db_session,
        job,
        save_failures=3,
        last_save_error="upload refused: storage quota full",
        last_save_error_at=datetime(2026, 11, 17, 3, 0, tzinfo=UTC),
    )

    page = " ".join(client.get(f"/projects/{project.id}/compute/{job.id}").text.split())

    assert "/workspace isn't saving" in page
    assert "The last 3 saves" in page and "storage quota full" in page


# -- The CLI's API ----------------------------------------------------------------------------------------------------


def _as(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {StubWeaveClient.cli_token_for(sub)}"}


BOX = {
    "name": "explore",
    "kind": "box",
    "image": "ubuntu:24.04",
    "gpu_names": ["RTX 3060"],
    "target_usd_per_hour": "0.07",
    "max_usd_per_hour": "0.10",
}


def test_the_api_continues_a_workspace_by_name_and_rerun_keeps_it(client: TestClient, project, db_session, store):
    first = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BOX, "workspace_name": "llm"}, headers=_as(MEMBER_SUB)
    ).json()
    assert first["workspace"]["name"] == "llm"
    client.post(f"/api/v1/jobs/{first['id']}/cancel", headers=_as(MEMBER_SUB))

    second = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BOX, "workspace": "llm"}, headers=_as(MEMBER_SUB)
    )
    unknown = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BOX, "workspace": "nope"}, headers=_as(MEMBER_SUB)
    )

    assert second.status_code == 201, second.text
    assert second.json()["workspace"] == first["workspace"]
    detail = client.get(f"/api/v1/jobs/{second.json()['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["spec"]["workspace"] == first["workspace"]["id"]
    assert unknown.status_code == 422 and "workspace" in unknown.json()["fields"]


def test_the_api_lists_downloads_and_deletes_workspaces(client: TestClient, project, db_session, store, login_as):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _saved(store, job)
    db_session.commit()  # the refused delete rolls back
    base = f"/api/v1/projects/{project.id}/workspaces"

    listed = client.get(base, headers=_as(MEMBER_SUB)).json()["workspaces"]
    files = client.get(f"{base}/{job.workspace_id}/files", headers=_as(MEMBER_SUB)).json()
    summary = client.get(f"/api/v1/projects/{project.id}/files", headers=_as(MEMBER_SUB)).json()
    held = client.delete(f"{base}/{job.workspace_id}", headers=_as(MEMBER_SUB))
    _ended(db_session, job)
    db_session.commit()
    deleted = client.delete(f"{base}/{job.workspace_id}", headers=_as(MEMBER_SUB))

    assert [(w["name"], w["holder"]["id"]) for w in listed] == [("explore", str(job.id))]
    entries = {e["path"]: e for e in files["entries"]}
    assert entries["train.py"]["mode"] == 493 and "expires=900" in entries["train.py"]["url"]
    assert entries["py"] == {
        "path": "py",
        "type": "link",
        "size_bytes": 0,
        "mode": 0,
        "mtime_ns": 0,
        "target": "/usr/bin/python3",
        "url": None,
    }
    assert summary["workspaces"][0]["stored"]["files"] == 3
    assert held.status_code == 409
    assert deleted.status_code == 200 and deleted.json() == {"deleted": 3}


def test_the_api_keeps_workspaces_from_outsiders_and_reviewers(
    client: TestClient, project, db_session, store, login_as
):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    login_as(OTHER_MEMBER_SUB)
    base = f"/api/v1/projects/{project.id}"

    assert client.get(f"{base}/workspaces", headers=_as(OTHER_MEMBER_SUB)).status_code == 404
    assert client.get(f"{base}/workspaces", headers=_as(REVIEWER_SUB)).status_code == 403
    assert client.get(f"{base}/files", headers=_as(REVIEWER_SUB)).status_code == 403
    assert client.get(f"{base}/workspaces/{job.workspace_id}/files", headers=_as(REVIEWER_SUB)).status_code == 403
    assert client.delete(f"{base}/workspaces/{job.workspace_id}", headers=_as(REVIEWER_SUB)).status_code == 403


def test_the_api_ends_a_box(client: TestClient, project, db_session, store, login_as):
    member = login_as(MEMBER_SUB)
    job = _box(db_session, project, member)
    _running(db_session, job)
    db_session.commit()

    response = client.post(f"/api/v1/jobs/{job.id}/end", headers=_as(MEMBER_SUB))

    assert response.status_code == 200 and response.json()["end_requested"] is True
