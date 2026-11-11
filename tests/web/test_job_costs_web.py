"""The price before a job runs (the form's "Estimate the cost", the job page, the API) and what it cost."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeInstance, ComputeInstanceState, ComputeJob, ComputeJobState
from krater.weave import StubWeaveClient
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.test_api import JOB
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


def test_the_form_estimates_without_queueing(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    assert "Estimate the cost" in page.text

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "data_in_gb": "100", "action": "estimate", "csrf_token": get_csrf_token(page.text)},
    )

    assert response.status_code == 200
    assert "What it may cost." in response.text
    assert "Up to $0.80 if it runs its full 8 hours" in response.text  # $0.10/h max x 8 h
    assert "$2.00 for the data you estimated" in response.text  # 100 GB at $20/TB
    assert db_session.scalars(sa.select(ComputeJob)).first() is None
    assert 'value="100"' in response.text  # the form comes back filled in


def test_the_job_page_shows_what_it_may_cost_and_what_it_cost(
    client: TestClient, login_as, project, db_session
) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    job.state = ComputeJobState.SUCCEEDED
    job.spend_cents = 5
    db_session.add(
        ComputeInstance(
            job_id=job.id,
            attempt=1,
            label="krater-job-c-1",
            provider_instance_id="321",
            state=ComputeInstanceState.DESTROYED,
            provider_offer_id="1",
            gpu_name="RTX 3060",
            interruptible=True,
            gpu_microusd=55_000,
            disk_microusd=8_767,
            accrued_microusd=45_000,
            est_gpu_microusd=40_000,
            est_disk_microusd=3_000,
            est_image_microusd=1_500,
            est_transfer_out_microusd=500,
            charged_items={"gpu": 41_000, "disk": 3_000, "bwd": 1_000, "bwu": 600},
        )
    )
    db_session.flush()

    page = client.get(f"/projects/{project.id}/compute/{job.id}").text

    assert "May cost" in page and "up to $0.80" in page
    assert "What it cost" in page
    assert "Data in (and image pulls)" in page and "$0.002" in page and "$0.001" in page
    assert "$0.041" in page  # Vast's GPU charge


def test_the_api_estimates_and_reports_the_parts(client: TestClient, project) -> None:
    estimate = client.post(
        f"/api/v1/projects/{project.id}/jobs/estimate", json={**JOB, "data_out_gb": 50}, headers=_as(MEMBER_SUB)
    )
    assert estimate.status_code == 200, estimate.text
    body = estimate.json()
    assert body["up_to_cents"] == 10 and body["data_cents"] == 100  # $0.10 x 1 h; 50 GB at $20/TB
    assert "Up to $0.10" in body["summary"]
    assert client.get("/api/v1/me", headers=_as(MEMBER_SUB)).status_code == 200

    queued = client.post(f"/api/v1/projects/{project.id}/jobs", json=JOB, headers=_as(MEMBER_SUB)).json()
    assert queued["estimate"]["up_to_cents"] == 10
    detail = client.get(f"/api/v1/jobs/{queued['id']}", headers=_as(MEMBER_SUB)).json()
    assert [part["label"] for part in detail["costs"]][:2] == ["GPU", "Disk"]
    assert detail["estimate"]["summary"]


def test_only_the_team_can_ask_for_an_estimate(client: TestClient, project) -> None:
    client.get("/api/v1/me", headers=_as(OTHER_MEMBER_SUB))
    response = client.post(f"/api/v1/projects/{project.id}/jobs/estimate", json=JOB, headers=_as(OTHER_MEMBER_SUB))
    assert response.status_code in (403, 404)


def test_a_bad_body_is_a_field_error(client: TestClient, project) -> None:
    response = client.post(
        f"/api/v1/projects/{project.id}/jobs/estimate", json={**JOB, "name": ""}, headers=_as(MEMBER_SUB)
    )
    assert response.status_code == 422 and "name" in response.json()["fields"]
