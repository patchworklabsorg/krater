"""Admin SSH access from a box's job page: the form, the banner the team sees, revoking, and who may do it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import AdminBoxAccess, ComputeInstance, ComputeInstanceState, ComputeJobKind, ComputeJobState
from krater.services import compute_jobs, ssh_keys
from krater.services.compute_jobs import JobSpec
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.conftest import actor_for


@pytest.fixture(autouse=True)
def vast_mode(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")


def _add_key(db_session, user) -> None:
    public = ed25519.Ed25519PrivateKey.generate().public_key()
    text = public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    ssh_keys.add_key(db_session, actor_for(user), name="laptop", text=text)


@pytest.fixture
def box(login_as, approved_project, db_session):
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    job = compute_jobs.submit_job(
        db_session,
        actor_for(member),
        project=project,
        spec=JobSpec(
            name="notebook",
            kind=ComputeJobKind.BOX,
            image="ubuntu:24.04",
            command=None,
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
            label="krtest-box-1",
            state=ComputeInstanceState.RUNNING,
            provider_offer_id="1",
            gpu_name="RTX 3060",
            interruptible=True,
            gpu_microusd=70_000,
            disk_microusd=5_000,
            ssh_host="198.51.100.7",
            ssh_port=41022,
            last_heartbeat={"phase": "box", "sessions": 0, "key_updates": True},
        )
    )
    db_session.flush()
    return job


def _page(client, job):
    return client.get(f"/projects/{job.project_id}/compute/{job.id}")


def _grant(client, job, reason="Reviewing the training code."):
    csrf = get_csrf_token(_page(client, job).text)
    return client.post(
        f"/projects/{job.project_id}/compute/{job.id}/admin-access",
        data={"csrf_token": csrf, "reason": reason},
        follow_redirects=False,
    )


def test_an_admin_gets_access_and_the_team_sees_it(client: TestClient, login_as, box, db_session) -> None:
    admin = login_as(ADMIN_SUB)
    _add_key(db_session, admin)
    assert "Get SSH access for 2 hours" in _page(client, box).text

    response = _grant(client, box)

    assert response.status_code == 303
    assert response.headers["location"].endswith("#admin-access")
    access = db_session.query(AdminBoxAccess).filter_by(job_id=box.id).one()
    assert access.admin_id == admin.id
    assert "End this access" in _page(client, box).text

    login_as(MEMBER_SUB)
    page = _page(client, box).text
    assert "An admin has SSH access to this box" in page
    assert "Reviewing the training code." in page
    assert "End this access" not in page
    assert "Get SSH access" not in page


def test_an_admin_without_a_key_is_told_to_add_one(client: TestClient, login_as, box) -> None:
    login_as(ADMIN_SUB)

    page = _page(client, box).text

    assert "Add an SSH key to your account first" in page
    assert "Get SSH access" not in page


def test_an_old_runner_is_explained(client: TestClient, login_as, box, db_session) -> None:
    _add_key(db_session, login_as(ADMIN_SUB))
    box.instances[0].last_heartbeat = {"phase": "box"}
    db_session.flush()

    assert "older than live key updates" in _page(client, box).text


def test_a_grant_without_a_reason_is_refused(client: TestClient, login_as, box, db_session) -> None:
    _add_key(db_session, login_as(ADMIN_SUB))

    _grant(client, box, reason="")

    assert db_session.query(AdminBoxAccess).count() == 0
    assert "A reason is required" in _page(client, box).text


def test_an_admin_ends_the_access(client: TestClient, login_as, box, db_session) -> None:
    _add_key(db_session, login_as(ADMIN_SUB))
    _grant(client, box)
    access = db_session.query(AdminBoxAccess).one()
    csrf = get_csrf_token(_page(client, box).text)

    response = client.post(
        f"/projects/{box.project_id}/compute/{box.id}/admin-access/{access.id}/revoke",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(access)
    assert access.revoked_at is not None
    assert "An admin has SSH access" not in _page(client, box).text


@pytest.mark.parametrize("sub", [MEMBER_SUB, REVIEWER_SUB])
def test_members_and_reviewers_cant_get_access(client: TestClient, login_as, box, db_session, sub) -> None:
    user = login_as(sub)
    _add_key(db_session, user)

    response = _grant(client, box)

    assert response.status_code == 403
    assert db_session.query(AdminBoxAccess).count() == 0


def test_the_heartbeat_route_sends_a_boxs_keys(client: TestClient, box, db_session) -> None:
    from krater.compute import runner_api

    row = box.instances[0]
    token = "t" * 40
    row.runner_token_hash = runner_api.hash_token(token)
    db_session.flush()

    response = client.post(
        "/runner/v1/heartbeat", json={"key_updates": True}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["ssh_keys"] == []  # the owner has no keys here, and no admin has access
