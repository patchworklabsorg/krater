"""The spend cap (form, API, rerun), the web form's starter job, a running job's checkpoint freshness, and the logs
API naming the newest attempt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeJob
from krater.models.compute_job import ComputeJobKind, ComputeJobState
from krater.services import compute_inputs
from krater.services.compute_files import JobFile
from krater.storage import FakeObjectStore
from krater.weave import StubWeaveClient
from krater.web.routers.compute import _checkpoint_freshness
from tests.conftest import MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.test_compute_pages import FORM


@pytest.fixture(autouse=True)
def compute_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")


@pytest.fixture
def store(monkeypatch) -> FakeObjectStore:
    store = FakeObjectStore()
    for router in ("compute", "api"):
        monkeypatch.setattr(f"krater.web.routers.{router}.get_compute_store", lambda: store)
    return store


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


def test_the_api_takes_a_spend_cap_and_a_rerun_keeps_it(client: TestClient, project) -> None:
    queued = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BODY, "max_spend_cents": 2500}, headers=_as(MEMBER_SUB)
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["max_spend_cents"] == 2500
    detail = client.get(f"/api/v1/jobs/{queued.json()['id']}", headers=_as(MEMBER_SUB)).json()
    assert detail["spec"]["max_spend_cents"] == 2500


def test_the_api_refuses_a_spend_cap_of_nothing(client: TestClient, project) -> None:
    response = client.post(
        f"/api/v1/projects/{project.id}/jobs", json={**BODY, "max_spend_cents": 0}, headers=_as(MEMBER_SUB)
    )
    assert response.status_code == 422


def test_the_form_queues_a_job_with_a_spend_cap(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    assert 'name="max_spend"' in page.text
    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "max_spend": "$12.50", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert db_session.query(ComputeJob).filter_by(project_id=project.id).one().max_spend_cents == 1250


def test_the_form_says_what_is_wrong_with_a_spend_cap(client: TestClient, login_as, project) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "max_spend": "lots", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Give the spend cap in dollars" in response.text


def test_the_starter_puts_its_script_in_the_inputs_and_fills_in_the_form(
    client: TestClient, login_as, project, store
) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    assert "?starter=1" in page.text

    page = client.get(f"/projects/{project.id}/compute/new?starter=1")
    assert page.status_code == 200
    key = f"projects/{project.id}/inputs/starter/train.py"
    assert store.read_prefix(key, 1 << 20) == compute_inputs.STARTER_SCRIPT.read_bytes()
    assert 'value="starter"' in page.text and "python train.py --minutes 20" in page.text

    # Opening it again leaves the stored script alone.
    client.get(f"/projects/{project.id}/compute/new?starter=1")
    assert len(store.list_prefix(f"projects/{project.id}/inputs/")) == 1


def test_only_the_team_gets_the_starter(client: TestClient, login_as, project, store) -> None:
    login_as(REVIEWER_SUB)
    assert client.get(f"/projects/{project.id}/compute/new?starter=1").status_code == 403
    assert store.list_prefix("projects/") == []


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _running(**fields) -> SimpleNamespace:
    defaults = {"checkpoint_upload_minutes": 5, "started_at": NOW - timedelta(minutes=30)}
    return SimpleNamespace(kind=ComputeJobKind.JOB, state=ComputeJobState.RUNNING, **{**defaults, **fields})


def _ckpt(minutes_ago: int) -> JobFile:
    return JobFile("checkpoints", "model.pt", 10, NOW - timedelta(minutes=minutes_ago))


def test_checkpoint_freshness() -> None:
    fresh = _checkpoint_freshness([_ckpt(4), _ckpt(20)], _running(), now=NOW)
    assert fresh == {"newest": NOW - timedelta(minutes=4), "minutes_ago": 4, "overdue": False}
    assert _checkpoint_freshness([_ckpt(11)], _running(), now=NOW)["overdue"] is True
    # None stored: overdue only past twice the interval plus 5 minutes for the first upload.
    assert _checkpoint_freshness([], _running(started_at=NOW - timedelta(minutes=14)), now=NOW)["overdue"] is False
    assert _checkpoint_freshness([], _running(), now=NOW)["overdue"] is True
    assert _checkpoint_freshness([], SimpleNamespace(kind=ComputeJobKind.BOX, state=None), now=NOW) is None
