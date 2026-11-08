"""Admin review of container images: /admin/images, inspecting an image, the inspection page, and the digest on a
job's page."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeInstance, ComputeInstanceState, ComputeJobState, ImageInspection
from krater.registry import FakeRegistry, HistoryStep, ImageDetails, get_registry
from krater.services import compute_jobs
from krater.services.compute_jobs import JobSpec
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import actor_for

IMAGE = "ghcr.io/alice/train:v3"
DIGEST = "sha256:" + "e" * 64
PINNED = f"ghcr.io/alice/train@{DIGEST}"
DETAILS = ImageDetails(
    digest=DIGEST,
    platform_digest=None,
    platform="linux/amd64",
    created="2026-11-01T10:00:00Z",
    size_bytes=3 << 20,
    layers=4,
    entrypoint=["/entrypoint.sh"],
    cmd=["python", "train.py"],
    user="1000",
    env=["HF_HOME=/cache"],
    labels={"org.opencontainers.image.source": "https://github.com/alice/train"},
    history=[HistoryStep(created="2026-11-01T09:30:00Z", created_by="RUN pip install torch", empty_layer=False)],
)


@pytest.fixture
def registry(client) -> FakeRegistry:
    fake = FakeRegistry()
    fake.add(IMAGE, DETAILS)
    client.app.dependency_overrides[get_registry] = lambda: fake
    return fake


@pytest.fixture
def job(login_as, approved_project, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    job = compute_jobs.submit_job(
        db_session,
        actor_for(member),
        project=project,
        spec=JobSpec(
            name="train",
            image=IMAGE,
            command="python train.py",
            gpu_names=("RTX 3060",),
            target_microusd=70_000,
            max_microusd=100_000,
        ),
        cap_cents=500,
        now=datetime.now(UTC),
        weave_client=StubWeaveClient(),
    )
    job.state = ComputeJobState.RUNNING
    db_session.add(
        ComputeInstance(
            job=job,
            attempt=1,
            label="krtest-img-1",
            state=ComputeInstanceState.RUNNING,
            provider_offer_id="1",
            gpu_name="RTX 3060",
            interruptible=True,
            gpu_microusd=70_000,
            disk_microusd=5_000,
            image_digest=DIGEST,
            image_pinned=True,
        )
    )
    db_session.flush()
    return job


def _inspect(client, image):
    csrf = get_csrf_token(client.get("/admin/images").text)
    return client.post("/admin/images/inspect", data={"csrf_token": csrf, "image": image}, follow_redirects=False)


def test_the_images_page_needs_an_admin(client: TestClient, login_as) -> None:
    assert client.get("/admin/images", follow_redirects=False).status_code == 303
    login_as(REVIEWER_SUB)
    assert client.get("/admin/images").status_code == 403


def test_the_images_page_lists_images_and_the_digests_machines_got(client: TestClient, login_as, job) -> None:
    login_as(ADMIN_SUB)

    page = client.get("/admin/images").text

    assert IMAGE in page
    assert DIGEST[:19] in page
    assert "pinned: exactly this ran" in page
    assert f'value="{PINNED}"' in page  # the Inspect button asks for exactly that digest


def test_inspecting_a_digest_stores_and_shows_it(client: TestClient, login_as, job, registry, db_session) -> None:
    login_as(ADMIN_SUB)

    response = _inspect(client, PINNED)

    assert response.status_code == 303
    row = db_session.query(ImageInspection).one()
    assert response.headers["location"] == f"/admin/images/inspections/{row.id}"
    assert registry.calls == [("inspect", PINNED)]
    page = client.get(response.headers["location"]).text
    assert "/entrypoint.sh" in page
    assert "RUN pip install torch" in page
    assert "HF_HOME=/cache" in page
    assert "https://github.com/alice/train" in page
    assert "Pinned: exactly this" in page  # the job that got this digest
    assert f"/compute/{job.id}" in page
    # The images page now links the inspection instead of asking again.
    assert f"/admin/images/inspections/{row.id}" in client.get("/admin/images").text


def test_a_registry_error_comes_back_as_a_message(client: TestClient, login_as, registry, db_session) -> None:
    login_as(ADMIN_SUB)
    registry.fail = "ghcr.io refused Krater (401): the image may be private."

    response = _inspect(client, IMAGE)

    assert response.headers["location"] == "/admin/images"
    assert "the image may be private" in client.get("/admin/images").text
    assert db_session.query(ImageInspection).count() == 0


@pytest.mark.parametrize("sub", [MEMBER_SUB, REVIEWER_SUB])
def test_only_admins_can_inspect(client: TestClient, login_as, registry, sub) -> None:
    login_as(sub)
    csrf = get_csrf_token(client.get("/").text)

    response = client.post("/admin/images/inspect", data={"csrf_token": csrf, "image": IMAGE})

    assert response.status_code == 404
    assert registry.calls == []


def test_the_job_page_shows_the_digest_and_admins_can_inspect_it(client: TestClient, login_as, job) -> None:
    login_as(MEMBER_SUB)
    member_page = client.get(f"/projects/{job.project_id}/compute/{job.id}").text
    assert DIGEST[:19] in member_page
    assert "pinned: exactly this image runs" in member_page
    assert "Inspect this image" not in member_page

    login_as(ADMIN_SUB)
    admin_page = client.get(f"/projects/{job.project_id}/compute/{job.id}").text
    assert "Inspect this image" in admin_page
    assert f'value="{PINNED}"' in admin_page
