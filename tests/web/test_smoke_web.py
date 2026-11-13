"""Smoke tests on the web and the API: the form's tick box, the job page's row, and `smoke_minutes` in the API."""

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


def test_the_api_queues_a_smoke_test_and_its_spec_reruns_it(client: TestClient, project, db_session) -> None:
    queued = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BODY, "smoke_minutes": 5}, headers=_as(MEMBER_SUB)
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["smoke_minutes"] == 5

    detail = client.get(f"/api/v1/jobs/{queued.json()['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["smoke_minutes"] == 5
    assert detail["spec"]["smoke_minutes"] == 5 and detail["spec"]["max_runtime_minutes"] == 240
    assert detail["estimate"]["up_to_cents"] == 3  # $0.09/h for the 15-minute backstop, not 4 hours


def test_an_ordinary_jobs_spec_has_no_smoke_test(client: TestClient, project) -> None:
    queued = client.post(f"/api/v1/projects/{project.id}/jobs", json=BODY, headers=_as(MEMBER_SUB)).json()
    detail = client.get(f"/api/v1/jobs/{queued['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["smoke_minutes"] is None and "smoke_minutes" not in detail["spec"]


def test_the_api_refuses_a_smoke_test_that_isnt_one(client: TestClient, project) -> None:
    response = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BODY, "smoke_minutes": 60}, headers=_as(MEMBER_SUB)
    )
    assert response.status_code == 422
    assert response.json()["fields"]["smoke_minutes"] == "Choose a smoke test of 1 to 30 minutes."


def test_the_form_queues_a_smoke_test_when_ticked(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    assert 'name="smoke"' in page.text and "Smoke test first" in page.text

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "smoke": "on", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    job = db_session.query(ComputeJob).filter_by(project_id=project.id).one()
    assert job.smoke_minutes == 5


def test_the_job_page_says_it_is_a_smoke_test(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member, smoke_minutes=5)

    page = client.get(f"/projects/{project.id}/compute/{job.id}").text

    assert "<dt>Smoke test</dt>" in page and "stopped after 5 minutes of running" in page
    form = client.get(f"/projects/{project.id}/compute/new?from={job.id}").text
    assert 'name="smoke" checked' in form
