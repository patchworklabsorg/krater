"""The member's compute pages: a project's jobs, the submit form, a job's page and its actions, and price history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import (
    ComputeInstance,
    ComputeInstanceState,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    ComputeOutbid,
    GpuPriceSample,
)
from krater.services import compute_jobs
from krater.services.compute_jobs import JobSpec
from krater.web.charts import PricePoint, price_chart_svg
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import actor_for

FORM = {
    "name": "train",
    "kind": "job",
    "image": "ghcr.io/alice/train:v3",
    "command": "python train.py",
    "gpu": "RTX 3060",
    "num_gpus": "1",
    "disk_gb": "32",
    "target": "0.07",
    "max": "0.10",
    "interruptible": "on",
    "max_wait_minutes": "720",
    "on_max_wait": "keep_waiting",
    "max_runtime_minutes": "480",
}


@pytest.fixture(autouse=True)
def vast_mode(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")


@pytest.fixture
def project(login_as, approved_project):
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    return approved_project(member, reviewer)


def _job(db_session, project, member, **overrides) -> ComputeJob:
    spec = {
        "name": "train",
        "image": "ghcr.io/alice/train:v3",
        "command": "python train.py",
        "gpu_names": ("RTX 3060",),
        "target_microusd": 70_000,
        "max_microusd": 100_000,
        **overrides,
    }
    return compute_jobs.submit_job(
        db_session, actor_for(member), project=project, spec=JobSpec(**spec), cap_cents=500, now=datetime.now(UTC)
    )


def test_the_pages_exist_only_with_compute_on_vast(client: TestClient, login_as, project, monkeypatch) -> None:
    login_as(MEMBER_SUB)
    monkeypatch.setattr(get_settings(), "compute_mode", "off")

    assert client.get(f"/projects/{project.id}/compute").status_code == 404


def test_the_team_reviewers_and_admins_see_a_projects_jobs(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    _job(db_session, project, member, name="nightly-train")

    assert "nightly-train" in client.get(f"/projects/{project.id}/compute").text
    login_as(REVIEWER_SUB)
    assert "nightly-train" in client.get(f"/projects/{project.id}/compute").text
    login_as(ADMIN_SUB)
    assert "nightly-train" in client.get(f"/projects/{project.id}/compute").text
    login_as(OTHER_MEMBER_SUB)
    assert client.get(f"/projects/{project.id}/compute").status_code == 404


def test_the_project_page_links_to_compute(client: TestClient, login_as, project) -> None:
    login_as(MEMBER_SUB)

    assert f"/projects/{project.id}/compute" in client.get(f"/projects/{project.id}").text


def test_a_member_queues_a_job(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")
    assert page.status_code == 200

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    job = db_session.scalars(sa.select(ComputeJob).where(ComputeJob.project_id == project.id)).one()
    assert job.state is ComputeJobState.QUEUED
    assert (job.target_microusd, job.max_microusd, job.interruptible) == (70_000, 100_000, True)
    assert job.gpu_names == ["RTX 3060"]
    assert response.headers["location"] == f"/projects/{project.id}/compute/{job.id}"


def test_a_bad_form_is_shown_again_with_the_errors(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "target": "0.20", "max": "0.10", "csrf_token": get_csrf_token(page.text)},
    )

    assert response.status_code == 422
    assert "The target can&#39;t be above the max price." in response.text
    assert 'value="ghcr.io/alice/train:v3"' in response.text
    assert db_session.scalars(sa.select(ComputeJob)).all() == []


def test_the_form_offers_a_limit_order_first(client: TestClient, login_as, project) -> None:
    login_as(MEMBER_SUB)

    page = client.get(f"/projects/{project.id}/compute/new")

    assert 'name="order" value="limit" data-role="order-choice" checked' in page.text
    assert "/static/js/compute-form.js" in page.text


def test_a_member_places_a_limit_order(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")

    response = client.post(
        f"/projects/{project.id}/compute",
        data={
            **FORM,
            "order": "limit",
            "limit": "0.08",
            "target": "",
            "max": "",
            "csrf_token": get_csrf_token(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    job = db_session.scalars(sa.select(ComputeJob).where(ComputeJob.project_id == project.id)).one()
    assert (job.target_microusd, job.max_microusd, job.on_outbid) == (80_000, 80_000, ComputeOutbid.HOLD)
    assert job.is_limit_order
    shown = client.get(response.headers["location"])
    assert "limit order at $0.0800/h" in shown.text


def test_a_limit_over_the_cap_is_shown_on_the_limit(client: TestClient, login_as, project, db_session) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute/new")

    response = client.post(
        f"/projects/{project.id}/compute",
        data={**FORM, "order": "limit", "limit": "900", "csrf_token": get_csrf_token(page.text)},
    )

    assert response.status_code == 422
    assert 'name="order" value="limit" data-role="order-choice" checked' in response.text
    assert "hourly cap" in response.text.split('id="job-limit"')[1].split("</div>")[0]


def test_submitting_needs_the_csrf_token(client: TestClient, login_as, project) -> None:
    login_as(MEMBER_SUB)

    assert client.post(f"/projects/{project.id}/compute", data=FORM).status_code == 403


def test_only_the_team_can_open_the_form_or_submit(client: TestClient, login_as, project) -> None:
    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")

    assert client.get(f"/projects/{project.id}/compute/new").status_code == 403
    response = client.post(f"/projects/{project.id}/compute", data={**FORM, "csrf_token": get_csrf_token(page.text)})
    assert response.status_code == 403


def test_a_box_page_shows_how_to_connect_and_can_be_stopped(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member, kind=ComputeJobKind.BOX, command=None)
    job.state = ComputeJobState.RUNNING
    db_session.add(
        ComputeInstance(
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
            ssh_host="198.51.100.7",
            ssh_port=41022,
        )
    )
    db_session.flush()

    page = client.get(f"/projects/{project.id}/compute/{job.id}")
    assert "ssh -p 41022 root@198.51.100.7" in page.text
    assert "$0.0638/h" in page.text

    response = client.post(
        f"/projects/{project.id}/compute/{job.id}/stop",
        data={"csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(job)
    assert job.instances[0].stop_requested_at is not None


def test_cancel_from_the_job_page(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    page = client.get(f"/projects/{project.id}/compute/{job.id}")

    client.post(f"/projects/{project.id}/compute/{job.id}/cancel", data={"csrf_token": get_csrf_token(page.text)})

    db_session.refresh(job)
    assert job.state is ComputeJobState.CANCELLED


def test_an_outsider_cannot_act_on_a_job(client: TestClient, login_as, project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    login_as(OTHER_MEMBER_SUB)
    page = client.get("/pricing/history")

    response = client.post(
        f"/projects/{project.id}/compute/{job.id}/cancel", data={"csrf_token": get_csrf_token(page.text)}
    )

    assert response.status_code == 404
    db_session.refresh(job)
    assert job.state is ComputeJobState.QUEUED


def test_price_history_draws_the_chart_and_a_table(client: TestClient, db_session) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    for n in range(6):
        db_session.add(
            GpuPriceSample(
                sampled_at=now - timedelta(hours=n),
                gpu_name="RTX 4090",
                rental_type="bid",
                num_gpus=1,
                disk_gb=32,
                offer_count=40,
                truncated=False,
                min_microusd=200_000 + n * 1000,
                p10_microusd=220_000,
                median_microusd=260_000,
            )
        )
    db_session.flush()

    page = client.get("/pricing/history?gpu=RTX%204090&rental=bid&days=1")

    assert page.status_code == 200
    assert '<svg class="chart"' in page.text and "chart__band" in page.text
    assert "Cheapest offer" in page.text and "$0.2000/h" in page.text
    assert "Not enough samples" in client.get("/pricing/history?gpu=H200").text


def test_the_chart_needs_two_points() -> None:
    now = datetime(2026, 10, 11, tzinfo=UTC)
    assert price_chart_svg([PricePoint(now, 1, 2, 3)], title="t") == ""
    svg = price_chart_svg(
        [PricePoint(now, 100_000, 120_000, 150_000), PricePoint(now + timedelta(hours=1), 90_000, 110_000, 140_000)],
        title="RTX & co",
    )
    assert 'aria-label="RTX &amp; co"' in svg
    assert svg.count("<polyline") == 1 and svg.count("<polygon") == 1


# -- Project secrets -------------------------------------------------------------------------------------------


@pytest.fixture
def secrets_key(monkeypatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(get_settings(), "project_secrets_key", Fernet.generate_key().decode())


def test_the_team_saves_a_secret_and_never_sees_it_again(
    client: TestClient, login_as, project, db_session, secrets_key
) -> None:
    from krater.services import project_secrets

    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute")
    assert "Save secret" in page.text

    response = client.post(
        f"/projects/{project.id}/compute/secrets",
        data={"name": "HF_TOKEN", "value": "hf_abc123", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert project_secrets.environment(db_session, project.id, get_settings()) == {"HF_TOKEN": "hf_abc123"}
    shown = client.get(f"/projects/{project.id}/compute").text
    assert "HF_TOKEN" in shown and "hf_abc123" not in shown


def test_a_bad_secret_name_is_shown_again_without_its_value(client: TestClient, login_as, project, secrets_key) -> None:
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}/compute")

    response = client.post(
        f"/projects/{project.id}/compute/secrets",
        data={"name": "KRATER_JOB_TOKEN", "value": "hf_abc123", "csrf_token": get_csrf_token(page.text)},
    )

    assert response.status_code == 422
    assert "used by Krater" in response.text
    assert "hf_abc123" not in response.text


def test_reviewers_see_secret_names_but_cannot_change_them(
    client: TestClient, login_as, project, db_session, secrets_key
) -> None:
    from krater.services import project_secrets

    member = login_as(MEMBER_SUB)
    project_secrets.set_secret(
        db_session, actor_for(member), project=project, name="HF_TOKEN", value="v", settings=get_settings()
    )
    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}/compute")
    assert "HF_TOKEN" in page.text and "Save secret" not in page.text

    token = get_csrf_token(page.text)
    set_response = client.post(
        f"/projects/{project.id}/compute/secrets", data={"name": "X", "value": "v", "csrf_token": token}
    )
    delete_response = client.post(f"/projects/{project.id}/compute/secrets/HF_TOKEN/delete", data={"csrf_token": token})

    assert set_response.status_code == 403 and delete_response.status_code == 403
    assert [s.name for s in project_secrets.list_secrets(db_session, project)] == ["HF_TOKEN"]


def test_the_team_deletes_a_secret(client: TestClient, login_as, project, db_session, secrets_key) -> None:
    from krater.services import project_secrets

    member = login_as(MEMBER_SUB)
    project_secrets.set_secret(
        db_session, actor_for(member), project=project, name="HF_TOKEN", value="v", settings=get_settings()
    )
    page = client.get(f"/projects/{project.id}/compute")

    response = client.post(
        f"/projects/{project.id}/compute/secrets/HF_TOKEN/delete",
        data={"csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert project_secrets.list_secrets(db_session, project) == []


def test_without_a_key_the_page_says_secrets_are_off(client: TestClient, login_as, project) -> None:
    login_as(MEMBER_SUB)

    page = client.get(f"/projects/{project.id}/compute")

    assert "Secrets aren't set up on this Krater yet" in page.text
    assert "Save secret" not in page.text


# -- Output and files ----------------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch):
    from krater.storage.fake import FakeObjectStore

    fake = FakeObjectStore()
    monkeypatch.setattr("krater.web.routers.compute.get_compute_store", lambda: fake)
    return fake


def _stored(store, job, area, name, content: bytes) -> None:
    key = f"projects/{job.project_id}/jobs/{job.id}/{area}/{name}"
    store.put(key, content_type="", size_bytes=len(content), content=content)


def test_the_job_page_shows_the_output_and_the_files(client: TestClient, login_as, project, db_session, store) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    _stored(store, job, "logs", "attempt-1.log", b"epoch 1 loss 0.42\n<b>not markup</b>\n")
    _stored(store, job, "outputs", "eval/score.txt", b"0.93\n")

    page = client.get(f"/projects/{project.id}/compute/{job.id}").text

    assert "epoch 1 loss 0.42" in page
    assert "&lt;b&gt;not markup&lt;/b&gt;" in page
    assert f"/projects/{project.id}/compute/{job.id}/files/outputs/eval/score.txt" in page
    assert f"krater download {job.id}" in page


def test_a_file_link_redirects_to_a_short_lived_url(client: TestClient, login_as, project, db_session, store) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    _stored(store, job, "outputs", "eval/score.txt", b"0.93\n")
    base = f"/projects/{project.id}/compute/{job.id}/files"

    response = client.get(f"{base}/outputs/eval/score.txt", follow_redirects=False)
    missing = client.get(f"{base}/outputs/nope", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"jobs/{job.id}/outputs/eval/score.txt?expires=900")
    assert missing.status_code == 404


def test_a_member_weave_dropped_can_no_longer_read_the_files(
    client: TestClient, login_as, project, db_session, store, weave_stub
) -> None:
    """Files are read with a live Weave check, not the roles from sign-in."""
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    _stored(store, job, "outputs", "x.txt", b"x")
    weave_stub.set_active(MEMBER_SUB, False)

    download = client.get(f"/projects/{project.id}/compute/{job.id}/files/outputs/x.txt", follow_redirects=False)

    assert download.status_code == 403


def test_reviewers_see_the_job_but_not_its_output(client: TestClient, login_as, project, db_session, store) -> None:
    member = login_as(MEMBER_SUB)
    job = _job(db_session, project, member)
    _stored(store, job, "logs", "attempt-1.log", b"private output\n")
    _stored(store, job, "outputs", "x.txt", b"x")
    login_as(REVIEWER_SUB)

    page = client.get(f"/projects/{project.id}/compute/{job.id}")
    download = client.get(f"/projects/{project.id}/compute/{job.id}/files/outputs/x.txt", follow_redirects=False)

    assert page.status_code == 200
    assert "private output" not in page.text
    assert download.status_code == 403
