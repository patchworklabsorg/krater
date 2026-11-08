"""An admin's SSH access to a running box (`krater.services.box_access`): the rules for granting it, and the key list
the runner gets on start and in every heartbeat."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import AuditEvent, ComputeJobState
from krater.services import box_access, ssh_keys
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from tests.compute.test_boxes import _beat, _running_box
from tests.compute.test_scheduler import NOW, _submit


def _key(db_session, actor) -> str:
    public = ed25519.Ed25519PrivateKey.generate().public_key()
    text = public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    ssh_keys.add_key(db_session, actor, name="laptop", text=text)
    return text


@pytest.fixture
def box(db_session, member, project, client, fake):  # noqa: F811
    """A running box whose runner takes key updates, owned by `member` (who has one key)."""
    owner_key = _key(db_session, member)
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3, key_updates=True)
    return job, row, owner_key


AT = NOW + timedelta(minutes=4)


def test_an_admins_keys_join_the_owners_while_access_lasts(db_session, admin, box) -> None:
    job, row, owner_key = box
    admin_key = _key(db_session, admin)

    access = box_access.grant(db_session, admin, job=job, reason="Checking the training code.", now=AT)

    assert access.expires_at == AT + timedelta(hours=box_access.ACCESS_HOURS)
    assert _beat(db_session, row, 5).ssh_keys == (owner_key, admin_key)
    assert runner_api.start(db_session, row, now=AT, settings=get_settings()).ssh_keys == (owner_key, admin_key)
    # Two hours on, the heartbeat drops the admin's key.
    later = runner_api.heartbeat(db_session, row, {"key_updates": True}, now=access.expires_at)
    assert later.ssh_keys == (owner_key,)

    event = db_session.query(AuditEvent).filter_by(action=box_access.AUDIT_GRANTED).one()
    assert event.reason == "Checking the training code."
    assert event.payload["job_id"] == str(job.id)
    assert event.project_id == job.project_id


def test_revoking_drops_the_key_at_the_next_heartbeat(db_session, admin, box, make_actor) -> None:
    job, row, owner_key = box
    _key(db_session, admin)
    access = box_access.grant(db_session, admin, job=job, reason="Review.", now=AT)
    other_admin = make_actor(groups=admin.groups)

    box_access.revoke(db_session, other_admin, access_id=access.id, job=job, now=AT + timedelta(minutes=10))

    assert _beat(db_session, row, 15).ssh_keys == (owner_key,)
    assert access.revoked_by_id == other_admin.user.id
    assert db_session.query(AuditEvent).filter_by(action=box_access.AUDIT_REVOKED).count() == 1
    with pytest.raises(InvalidState):
        box_access.revoke(db_session, admin, access_id=access.id, job=job, now=AT + timedelta(minutes=11))


def test_a_key_the_admin_removes_leaves_the_box(db_session, admin, box) -> None:
    job, row, owner_key = box
    _key(db_session, admin)
    box_access.grant(db_session, admin, job=job, reason="Review.", now=AT)

    for key in ssh_keys.list_keys(db_session, admin.user):
        ssh_keys.remove_key(db_session, admin, key_id=key.id)

    assert _beat(db_session, row, 5).ssh_keys == (owner_key,)


def test_only_admins_get_access(db_session, member, box) -> None:
    job, _, _ = box

    with pytest.raises(NotAllowed):
        box_access.grant(db_session, member, job=job, reason="Mine.", now=AT)


def test_access_needs_a_reason_and_a_key(db_session, admin, box) -> None:
    job, _, _ = box

    with pytest.raises(ValidationFailed) as no_reason:
        box_access.grant(db_session, admin, job=job, reason="  ", now=AT)
    assert "reason" in no_reason.value.errors
    with pytest.raises(ValidationFailed) as no_key:
        box_access.grant(db_session, admin, job=job, reason="Review.", now=AT)
    assert "SSH key" in no_key.value.errors["reason"]


def test_one_grant_at_a_time_per_admin(db_session, admin, box) -> None:
    job, _, _ = box
    _key(db_session, admin)
    box_access.grant(db_session, admin, job=job, reason="Review.", now=AT)

    with pytest.raises(InvalidState, match="already have access"):
        box_access.grant(db_session, admin, job=job, reason="Again.", now=AT + timedelta(minutes=1))


def test_only_a_running_box_with_a_new_runner(db_session, admin, member, project, client, fake) -> None:  # noqa: F811
    _key(db_session, admin)
    batch = _submit(db_session, member, project)
    with pytest.raises(InvalidState, match="running"):
        box_access.grant(db_session, admin, job=batch, reason="Review.", now=AT)

    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3)  # an older runner doesn't say key_updates
    with pytest.raises(InvalidState, match="older than live key updates"):
        box_access.grant(db_session, admin, job=job, reason="Review.", now=AT)

    _beat(db_session, row, 4, key_updates=True)
    job.state = ComputeJobState.STOPPED
    with pytest.raises(InvalidState, match="running"):
        box_access.grant(db_session, admin, job=job, reason="Review.", now=AT)


def test_revoking_needs_the_grant_on_this_box(db_session, admin, box) -> None:
    import uuid

    job, _, _ = box

    with pytest.raises(NotFound):
        box_access.revoke(db_session, admin, access_id=uuid.uuid4(), job=job, now=AT)


def test_a_batch_job_heartbeat_carries_no_keys(db_session, member, project, client, fake) -> None:  # noqa: F811
    from tests.compute.test_scheduler import _instance, _pass, _token

    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    instance = runner_api.authenticate(db_session, _token(fake, row))

    assert runner_api.heartbeat(db_session, instance, {}, now=AT).ssh_keys is None


def test_the_heartbeat_keeps_the_runners_key_updates_flag(db_session, box) -> None:
    _, row, _ = box

    assert row.last_heartbeat["key_updates"] is True
    _beat(db_session, row, 5, key_updates="yes")  # wrong type: dropped
    assert "key_updates" not in row.last_heartbeat
