"""Admins' SSH access to every box, as the job page and the new-box form show it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeInstance, ComputeInstanceState, ComputeJobKind, ComputeJobState
from krater.services import box_access, compute_jobs, ssh_keys
from krater.services.compute_jobs import JobSpec
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
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


def _sync(db_session, weave_stub) -> None:
    box_access.sync_admins(db_session, weave_stub, now=datetime.now(UTC))


def test_the_team_sees_which_admins_can_log_in(client: TestClient, login_as, box, db_session, weave_stub) -> None:
    login_as(ADMIN_SUB)
    login_as(MEMBER_SUB)
    assert "None are on this one right now" in _page(client, box).text

    _sync(db_session, weave_stub)

    page = _page(client, box).text
    assert "On this one: Ana Admin." in page
    assert "can read the project's secrets" in page
    assert "Your keys" not in page  # that line is for admins


def test_an_admin_without_a_key_is_told_to_add_one(client: TestClient, login_as, box, db_session, weave_stub) -> None:
    login_as(ADMIN_SUB)
    _sync(db_session, weave_stub)

    assert "Add an SSH key to your account to log in" in _page(client, box).text


def test_an_admin_with_a_key_is_on_the_box(client: TestClient, login_as, box, db_session, weave_stub) -> None:
    _add_key(db_session, login_as(ADMIN_SUB))
    assert "Your keys go on within about five minutes" in _page(client, box).text

    _sync(db_session, weave_stub)

    assert "Your keys are on it" in _page(client, box).text


def test_an_old_runner_is_explained(client: TestClient, login_as, box, db_session, weave_stub) -> None:
    _add_key(db_session, login_as(ADMIN_SUB))
    _sync(db_session, weave_stub)
    box.instances[0].last_heartbeat = {"phase": "box"}
    db_session.flush()

    assert "older than live key updates" in _page(client, box).text


def test_a_batch_jobs_page_has_no_admin_ssh_card(client: TestClient, login_as, box, db_session) -> None:
    login_as(MEMBER_SUB)
    box.kind = ComputeJobKind.JOB
    box.command = "python train.py"
    db_session.flush()

    assert "Admin SSH access" not in _page(client, box).text


def test_the_old_request_form_is_gone(client: TestClient, login_as, box, db_session) -> None:
    _add_key(db_session, login_as(ADMIN_SUB))
    page = _page(client, box)
    assert "Get SSH access" not in page.text

    response = client.post(
        f"/projects/{box.project_id}/compute/{box.id}/admin-access",
        data={"csrf_token": get_csrf_token(page.text), "reason": "Old form."},
        follow_redirects=False,
    )

    assert response.status_code in (404, 405)


def test_the_new_box_form_says_admins_can_log_in(client: TestClient, login_as, box) -> None:
    login_as(MEMBER_SUB)

    page = client.get(f"/projects/{box.project_id}/compute/new").text

    assert "Krater admins' SSH keys go on every box too" in page


@pytest.mark.parametrize("sub", [OTHER_MEMBER_SUB, REVIEWER_SUB])
def test_members_and_reviewers_keys_stay_off_other_boxes(
    client: TestClient, login_as, box, db_session, weave_stub, sub
) -> None:
    _add_key(db_session, login_as(sub))
    _sync(db_session, weave_stub)

    assert box_access.authorized_keys(db_session, box, now=datetime.now(UTC)) == []


def test_the_heartbeat_route_sends_the_admins_keys(client: TestClient, login_as, box, db_session, weave_stub) -> None:
    from krater.compute import runner_api

    _add_key(db_session, login_as(ADMIN_SUB))
    _sync(db_session, weave_stub)
    row = box.instances[0]
    token = "t" * 40
    row.runner_token_hash = runner_api.hash_token(token)
    db_session.flush()

    response = client.post(
        "/runner/v1/heartbeat", json={"key_updates": True}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert len(response.json()["ssh_keys"]) == 1  # the owner has no keys here; the admin has one
