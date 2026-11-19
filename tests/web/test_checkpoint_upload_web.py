"""A job's checkpoint upload interval on the web and the API: the form's field, the job page's row, and
`checkpoint_upload_minutes` in the API (kept by a rerun)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeJob
from krater.weave import StubWeaveClient
from tests.conftest import MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.test_compute_pages import FORM, _job


@pytest.fixture(autouse=True)
def compute_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")


@pytest.fixture
def project(login_as, approved_project):
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    return approved_project(member, reviewer)


def _as(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {StubWeaveClient.cli_token_for(sub)}"}


BODY = {
    "name": "train",
    "image": "ghcr.io/alice/train:v3",
    "command": "python train.py",
    "gpu_names": ["RTX 3060"],
    "limit_usd_per_hour": "0.09",
    "max_runtime_minutes": 240,
}


def test_the_api_takes_an_interval_and_its_spec_keeps_it(client: TestClient, project) -> None:
    queued = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BODY, "checkpoint_upload_minutes": 2}, headers=_as(MEMBER_SUB)
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["checkpoint_upload_minutes"] == 2
    assert queued.json()["checkpoint_upload_minutes_effective"] == 2

    detail = client.get(f"/api/v1/jobs/{queued.json()['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["spec"]["checkpoint_upload_minutes"] == 2


def test_a_job_without_one_reports_the_default(client: TestClient, project) -> None:
    queued = client.post(f"/api/v1/projects/{project.id}/jobs", json=BODY, headers=_as(MEMBER_SUB)).json()
    detail = client.get(f"/api/v1/jobs/{queued['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["checkpoint_upload_minutes"] is None and detail["checkpoint_upload_minutes_effective"] == 10
    assert "checkpoint_upload_minutes" not in detail["spec"]


def test_the_api_refuses_an_interval_out_of_range(client: TestClient, project) -> None:
    response = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BODY, "checkpoint_upload_minutes": 90}, headers=_as(MEMBER_SUB)
    )
    assert response.status_code == 422
    assert response.json()["fields"]["checkpoint_upload_minutes"].startswith("Choose a checkpoint upload every 1 to 60")


def test_the_form_queues_a_job_with_its_interval(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    assert 'name="checkpoint_upload_minutes"' in page.text and "Upload checkpoints every" in page.text

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "checkpoint_upload_minutes": "3", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    job = db_session.query(ComputeJob).filter_by(project_id=project.id).one()
    assert job.checkpoint_upload_minutes == 3


def test_a_blank_field_keeps_the_default(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "checkpoint_upload_minutes": "", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert db_session.query(ComputeJob).filter_by(project_id=project.id).one().checkpoint_upload_minutes is None


def test_the_form_says_what_is_wrong_with_the_interval(client: TestClient, login_as, project) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "checkpoint_upload_minutes": "often", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Enter a whole number for checkpoint upload interval." in response.text


def test_the_job_page_says_how_often_it_uploads(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member, checkpoint_upload_minutes=2)

    page = client.get(f"/projects/{project.id}/compute/{job.id}").text

    assert "<dt>Checkpoint uploads</dt>" in page and "go up every 2 minutes" in page
    form = client.get(f"/projects/{project.id}/compute/new?from={job.id}").text
    assert 'name="checkpoint_upload_minutes" placeholder="10" value="2"' in form


def test_the_job_page_names_the_default(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    page = client.get(f"/projects/{project.id}/compute/{job.id}").text
    assert "go up every 10 minutes (the default)" in page
