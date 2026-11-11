"""A project's inputs: uploading, listing and deleting them through the API, and jobs that name them."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.compute import scheduler
from krater.config import get_settings
from krater.models import ComputeJob
from krater.storage import FakeObjectStore
from krater.weave import StubWeaveClient
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.test_api import JOB
from tests.web.test_compute_pages import FORM

GB = 1_000_000_000


@pytest.fixture(autouse=True)
def compute_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")


@pytest.fixture
def store(monkeypatch) -> FakeObjectStore:
    store = FakeObjectStore()
    for router in ("api", "compute"):
        monkeypatch.setattr(f"krater.web.routers.{router}.get_compute_store", lambda: store)
    return store


@pytest.fixture
def project(login_as, approved_project):
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    return approved_project(member, reviewer)


def _as(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {StubWeaveClient.cli_token_for(sub)}"}


def _stored(store: FakeObjectStore, project, name: str, size: int) -> None:
    store.put(f"projects/{project.id}/inputs/{name}", content_type="", size_bytes=size)


def test_the_team_uploads_lists_and_deletes_inputs(client: TestClient, project, store) -> None:
    base = f"/api/v1/projects/{project.id}/inputs"

    small = client.post(f"{base}/upload", json={"name": "data/a.csv", "size": 10}, headers=_as(MEMBER_SUB))
    big = client.post(f"{base}/upload", json={"name": "data/big.bin", "size": 100 * 1024**2}, headers=_as(MEMBER_SUB))

    assert small.status_code == 200 and "url" in small.json()
    assert len(big.json()["parts"]) == 2 and big.json()["upload_id"]
    _stored(store, project, "data/a.csv", 10)
    _stored(store, project, "vocab.txt", 5)
    listed = client.get(base, headers=_as(MEMBER_SUB)).json()["inputs"]
    assert [f["name"] for f in listed] == ["data/a.csv", "vocab.txt"]

    deleted = client.delete(f"{base}/data/", headers=_as(MEMBER_SUB))
    assert deleted.json() == {"deleted": 1}
    assert [f["name"] for f in client.get(base, headers=_as(MEMBER_SUB)).json()["inputs"]] == ["vocab.txt"]
    assert client.delete(f"{base}/nothing", headers=_as(MEMBER_SUB)).status_code == 404


def test_only_the_team_can_change_inputs(client: TestClient, project, store) -> None:
    client.get("/api/v1/me", headers=_as(OTHER_MEMBER_SUB))
    base = f"/api/v1/projects/{project.id}/inputs"
    _stored(store, project, "a.csv", 1)

    upload = client.post(f"{base}/upload", json={"name": "x", "size": 1}, headers=_as(OTHER_MEMBER_SUB))
    delete = client.delete(f"{base}/a.csv", headers=_as(OTHER_MEMBER_SUB))

    assert upload.status_code in (403, 404) and delete.status_code in (403, 404)
    assert [i.key for i in store.list_prefix("projects/")] == [f"projects/{project.id}/inputs/a.csv"]


def test_inputs_count_toward_the_storage_quota(client: TestClient, project, store, db_session) -> None:
    _stored(store, project, "huge.bin", 50 * 1024**3)  # the default quota is 50 GB
    response = client.post(
        f"/api/v1/projects/{project.id}/inputs/upload", json={"name": "more", "size": 1}, headers=_as(MEMBER_SUB)
    )
    assert response.status_code == 422 and "quota" in response.json()["fields"]["size"]


def test_a_job_names_its_inputs_and_their_size_counts_as_data_in(client: TestClient, project, store) -> None:
    _stored(store, project, "data/a.bin", 3 * GB)
    _stored(store, project, "data/b.bin", 2 * GB)
    body = {**JOB, "inputs": ["data/"]}

    estimate = client.post(f"/api/v1/projects/{project.id}/jobs/estimate", json=body, headers=_as(MEMBER_SUB))
    queued = client.post(f"/api/v1/projects/{project.id}/jobs", json=body, headers=_as(MEMBER_SUB))

    assert estimate.json()["data_cents"] == 10  # 5 GB at $20/TB
    assert queued.status_code == 201, queued.text
    detail = client.get(f"/api/v1/jobs/{queued.json()['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["inputs"] == ["data/"] and detail["inputs_bytes"] == 5 * GB
    assert detail["spec"]["inputs"] == ["data/"]


def test_a_job_naming_a_missing_input_is_refused(client: TestClient, project, store, db_session) -> None:
    response = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**JOB, "inputs": ["nope.tar"]}, headers=_as(MEMBER_SUB)
    )
    assert response.status_code == 422 and "nope.tar" in response.json()["fields"]["inputs"]
    assert db_session.scalars(sa.select(ComputeJob)).first() is None


def test_the_form_takes_inputs_and_the_pages_show_them(
    client: TestClient, login_as, project, store, db_session
) -> None:
    login_as(MEMBER_SUB)
    _stored(store, project, "vocab.txt", 2048)
    assert "vocab.txt" in client.get(f"/projects/{project.id}/compute").text
    page = client.get(f"/projects/{project.id}/compute/new")

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "inputs": "vocab.txt\n", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    job = db_session.scalars(sa.select(ComputeJob)).one()
    assert job.input_names == ["vocab.txt"] and job.inputs_bytes == 2048
    assert scheduler._transfer(job).inputs_bytes == 2048
    assert "$KRATER_INPUT_DIR" in client.get(response.headers["location"]).text
    again = client.get(f"/projects/{project.id}/compute/new?from={job.id}").text
    assert ">vocab.txt</textarea>" in again
