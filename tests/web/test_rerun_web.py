"""Running a job again: the form filled in from it ("Run again"), and the API's `spec` that `krater rerun` sends."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeEscalation, ComputeJobState, ComputeOutbid
from krater.weave import StubWeaveClient
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB
from tests.web.test_compute_pages import _job


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


def test_run_again_fills_in_the_form_from_the_job(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(
        db_session,
        project,
        member,
        min_gpu_ram_gb=24,
        gpu_names=(),
        data_in_gb=40,
        max_wait_minutes=90,
        on_max_wait=ComputeEscalation.CANCEL,
    )
    job.state = ComputeJobState.SUCCEEDED
    db_session.flush()

    page = client.get(f"/projects/{project.id}/compute/{job.id}").text
    assert f"/compute/new?from={job.id}" in page and "Run again" in page

    form = client.get(f"/projects/{project.id}/compute/new?from={job.id}")
    assert form.status_code == 200
    text = form.text
    assert "Filled in from" in text
    assert 'value="ghcr.io/alice/train:v3"' in text and "python train.py" in text
    assert 'name="min_vram" value="24"' in text and 'name="data_in_gb" value="40"' in text
    assert 'name="max_wait_minutes" value="90"' in text
    assert '<option value="cancel" selected' in text
    assert 'name="target" placeholder="0.12" value="0.07"' in text and 'value="0.1"' in text
    assert 'value="range" data-role="order-choice" checked' in text


def test_run_again_keeps_a_limit_order(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member, target_microusd=90_000, max_microusd=90_000, on_outbid=ComputeOutbid.HOLD)

    text = client.get(f"/projects/{project.id}/compute/new?from={job.id}").text

    assert 'value="limit" data-role="order-choice" checked' in text
    assert 'name="limit" placeholder="0.12" value="0.09"' in text


def test_run_again_only_takes_a_job_of_the_same_project(
    client: TestClient, login_as, approved_project, project, db_session
) -> None:
    other = login_as(OTHER_MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    theirs = approved_project(other, reviewer, title="Theirs")
    their_job = _job(db_session, theirs, other, command="secret-command --token x")

    login_as(MEMBER_SUB)
    response = client.get(f"/projects/{project.id}/compute/new?from={their_job.id}")

    assert response.status_code == 404
    assert "secret-command" not in response.text


def test_the_api_sends_the_jobs_settings_and_they_queue_it_again(client: TestClient, project, db_session) -> None:
    body = {
        "name": "train",
        "image": "ghcr.io/alice/train:v3",
        "command": "python train.py",
        "gpu_names": ["RTX 3060"],
        "limit_usd_per_hour": "0.09",
        "max_runtime_minutes": 60,
        "max_wait_minutes": 30,
        "on_max_wait": "cancel",
        "data_out_gb": 5,
    }
    queued = client.post(f"/api/v1/projects/{project.id}/jobs", json=body, headers=_as(MEMBER_SUB)).json()
    detail = client.get(f"/api/v1/jobs/{queued['id']}", headers=_as(MEMBER_SUB)).json()

    spec = detail["spec"]
    assert spec["limit_usd_per_hour"] == "0.0900" and "target_usd_per_hour" not in spec
    assert spec["on_outbid"] == "hold" and spec["on_max_wait"] == "cancel" and spec["max_wait_minutes"] == 30
    assert detail["min_gpu_ram_gb"] is None and detail["on_max_wait"] == "cancel"

    again = client.post(f"/api/v1/projects/{project.id}/jobs", json=spec, headers=_as(MEMBER_SUB))
    assert again.status_code == 201, again.text
    second = client.get(f"/api/v1/jobs/{again.json()['id']}", headers=_as(MEMBER_SUB)).json()
    assert second["spec"] == spec
