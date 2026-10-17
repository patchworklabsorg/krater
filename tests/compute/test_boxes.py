"""Interactive boxes against FakeVast: launch, SSH, idle stop with a workspace sync, resume, move, and the rules that
protect a stopped box's disk."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import ComputeEvent, ComputeInstanceState, ComputeJobKind, ComputeJobState
from krater.services import compute_jobs, ssh_keys
from tests.compute.test_scheduler import NOW, _instance, _pass, _submit, _token


def _box(db_session, member, project, **overrides):  # noqa: F811
    overrides.setdefault("kind", ComputeJobKind.BOX)
    overrides.setdefault("command", None)
    overrides.setdefault("max_runtime_minutes", 7 * 24 * 60)
    return _submit(db_session, member, project, **overrides)


def _running_box(db_session, member, project, client, fake):  # noqa: F811
    job = _box(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(
        row.provider_instance_id,
        "running",
        public_ipaddr="198.51.100.7",
        ports={"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41022"}]},
    )
    instance = runner_api.authenticate(db_session, _token(fake, row))
    runner_api.start(db_session, instance, now=NOW + timedelta(minutes=1), settings=get_settings())
    _pass(db_session, client, NOW + timedelta(minutes=2))
    return job, row


def _beat(db_session, row, minute, **beat):
    beat = {"gpu_util": 0, "sessions": 0, **beat}
    return runner_api.heartbeat(db_session, row, beat, now=NOW + timedelta(minutes=minute))


def _events(db_session, job) -> list[str]:
    return list(
        db_session.scalars(sa.select(ComputeEvent.kind).where(ComputeEvent.job_id == job.id).order_by(ComputeEvent.at))
    )


def test_a_box_launches_in_ssh_mode_with_its_runner_in_the_background(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job = _box(db_session, member, project)

    _pass(db_session, client)

    created = fake.instances[_instance(db_session, job).provider_instance_id]
    assert created["runtype"] == "ssh_direct"
    assert "nohup" in created["onstart"] and created["args"] is None
    assert fake.search_bodies[-1]["direct_port_count"] == {"gte": 1}


def test_no_boxes_while_the_vast_account_has_ssh_keys(db_session, member, project, client, fake) -> None:  # noqa: F811
    fake.account_ssh_keys = [{"id": 1, "public_key": "ssh-ed25519 AAAA someone"}]
    job = _box(db_session, member, project)

    _pass(db_session, client)

    assert job.state is ComputeJobState.QUEUED
    assert "SSH keys" in job.state_reason
    assert fake.instances == {}


def test_the_box_records_where_to_ssh_and_gets_its_owners_keys(db_session, member, project, client, fake) -> None:  # noqa: F811
    key = ed25519.Ed25519PrivateKey.generate().public_key()
    text = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    ssh_keys.add_key(db_session, member, name="laptop", text=text)

    job, row = _running_box(db_session, member, project, client, fake)

    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)
    info = runner_api.start(db_session, row, now=NOW + timedelta(minutes=3), settings=get_settings())
    assert info.ssh_keys == (text,)


def test_an_idle_box_saves_its_workspace_then_stops(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)

    for minute in range(3, 34):
        reply = _beat(db_session, row, minute)
        _pass(db_session, client, NOW + timedelta(minutes=minute))
    assert row.stop_requested_at is not None and job.state is ComputeJobState.RUNNING
    assert _beat(db_session, row, 34).sync is True

    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=34, seconds=30))
    _pass(db_session, client, NOW + timedelta(minutes=35))

    assert row.state is ComputeInstanceState.STOPPED and job.state is ComputeJobState.STOPPED
    assert fake.instances[row.provider_instance_id]["intended_status"] == "stopped"
    assert "saved" in job.state_reason and "not confirmed" not in job.state_reason
    del reply


def test_activity_keeps_a_box_running(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)

    for minute in range(3, 60):
        _beat(db_session, row, minute, sessions=1 if minute % 20 == 0 else 0, gpu_util=50 if minute % 25 == 0 else 0)
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert row.stop_requested_at is None and job.state is ComputeJobState.RUNNING


def test_a_runner_that_never_confirms_the_sync_is_stopped_at_the_deadline(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=5))

    _beat(db_session, row, 10)
    _pass(db_session, client, NOW + timedelta(minutes=10))
    assert row.state is ComputeInstanceState.RUNNING
    _beat(db_session, row, 15)
    _pass(db_session, client, NOW + timedelta(minutes=15))

    assert row.state is ComputeInstanceState.STOPPED
    assert "not confirmed saved" in job.state_reason


def test_a_stopped_box_bills_disk_only(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))
    before = row.accrued_microusd

    _pass(db_session, client, NOW + timedelta(minutes=64))

    assert row.accrued_microusd - before == row.disk_microusd


def test_resume_restarts_the_same_machine_and_the_runner_must_start_again(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))

    compute_jobs.resume_box(db_session, member, job=job, now=NOW + timedelta(minutes=10))
    _pass(db_session, client, NOW + timedelta(minutes=11))  # asks Vast to start it
    _pass(db_session, client, NOW + timedelta(minutes=12))  # sees it running

    assert row.state is ComputeInstanceState.RUNNING and job.state is ComputeJobState.RUNNING
    assert row.runner_starts_needed == row.runner_starts + 1
    runner_api.start(db_session, row, now=NOW + timedelta(minutes=12, seconds=20), settings=get_settings())
    for minute in range(13, 25):
        _beat(db_session, row, minute, sessions=1)
        _pass(db_session, client, NOW + timedelta(minutes=minute))
    assert job.state is ComputeJobState.RUNNING


def test_resume_when_the_gpu_is_taken_tells_the_member_and_keeps_the_disk(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))
    fake.gpu_taken[row.provider_instance_id] = True

    compute_jobs.resume_box(db_session, member, job=job, now=NOW + timedelta(minutes=10))
    for minute in range(11, 22):
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert job.state is ComputeJobState.STOPPED and row.state is ComputeInstanceState.STOPPED
    assert "GPU is taken" in job.state_reason and "workspace saved at" in job.state_reason
    assert row.provider_instance_id in fake.instances  # never destroyed on its own
    assert "resume_blocked" in _events(db_session, job)


def test_moving_a_stopped_box_starts_it_elsewhere_resumed(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))

    compute_jobs.move_box(db_session, member, job=job, now=NOW + timedelta(minutes=10))
    _pass(db_session, client, NOW + timedelta(minutes=11))

    assert row.state is ComputeInstanceState.DESTROYED
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.provider_instance_id in fake.instances
    assert "KRATER_RESUMED=1" in fake.instances[new.provider_instance_id]["env"]


def test_a_box_cancelled_while_moving_stays_cancelled(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))
    compute_jobs.move_box(db_session, member, job=job, now=NOW + timedelta(minutes=10))

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=10, seconds=30))
    _pass(db_session, client, NOW + timedelta(minutes=11))
    _pass(db_session, client, NOW + timedelta(minutes=12))

    assert row.state is ComputeInstanceState.DESTROYED
    assert job.state is ComputeJobState.CANCELLED
    assert _instance(db_session, job).id == row.id  # nothing rented again


def test_a_box_started_outside_krater_is_billed_and_stopped_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))
    _pass(db_session, client, NOW + timedelta(minutes=5))
    fake.set_status(row.provider_instance_id, "running", intended_status="running")  # the member used the container key
    before = row.accrued_microusd

    _pass(db_session, client, NOW + timedelta(minutes=65))

    assert row.state is ComputeInstanceState.STOPPED
    assert fake.instances[row.provider_instance_id]["intended_status"] == "stopped"
    assert row.accrued_microusd - before >= row.gpu_microusd  # an hour of GPU on top of the disk
    assert "unexpected_start" in _events(db_session, job)


def test_a_stopped_box_is_destroyed_after_the_destroy_time_with_a_warning_first(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))

    _pass(db_session, client, NOW + timedelta(minutes=4, hours=23, seconds=30))
    assert "destroy_soon" in _events(db_session, job) and row.state is ComputeInstanceState.STOPPED
    _pass(db_session, client, NOW + timedelta(minutes=5, hours=24))

    assert row.state is ComputeInstanceState.DESTROYED
    assert job.state is ComputeJobState.CANCELLED and "saved workspace is kept" in job.state_reason


def test_a_silent_runner_stops_a_box_instead_of_destroying_it(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3, sessions=1)

    for minute in range(3, 35):
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert row.state is ComputeInstanceState.STOPPED and job.state is ComputeJobState.STOPPED
    assert row.provider_instance_id in fake.instances


def test_a_box_that_stopped_itself_without_krater_shows_as_stopped(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3, sessions=1)
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=200))

    assert row.state is ComputeInstanceState.STOPPED and job.state is ComputeJobState.STOPPED
    assert "Stopped itself" in job.state_reason and "resume it" in job.state_reason
    assert row.provider_instance_id in fake.instances  # its disk is kept
    assert "self_stopped" in _events(db_session, job)


def test_only_boxes_stop_and_resume(db_session, member, project) -> None:  # noqa: F811
    import pytest

    from krater.services.errors import InvalidState

    job = _submit(db_session, member, project)
    with pytest.raises(InvalidState):
        compute_jobs.stop_box(db_session, member, job=job, now=NOW)


def test_max_runtime_counts_running_time_only(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project, max_runtime_minutes=8 * 60)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    runner_api.start(db_session, row, now=NOW + timedelta(minutes=1), settings=get_settings())
    for minute in range(2, 63):
        _beat(db_session, row, minute, sessions=1)
        _pass(db_session, client, NOW + timedelta(minutes=minute))
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=63))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=63, seconds=5))
    _pass(db_session, client, NOW + timedelta(minutes=64))
    assert row.state is ComputeInstanceState.STOPPED

    later = NOW + timedelta(hours=11)
    compute_jobs.resume_box(db_session, member, job=job, now=later)
    _pass(db_session, client, later + timedelta(minutes=1))
    _pass(db_session, client, later + timedelta(minutes=2))
    runner_api.start(db_session, row, now=later + timedelta(minutes=2, seconds=10), settings=get_settings())
    _beat(db_session, row, 11 * 60 + 3, sessions=1)
    _pass(db_session, client, later + timedelta(minutes=3))

    assert job.state is ComputeJobState.RUNNING and row.state is ComputeInstanceState.RUNNING


def test_a_box_at_its_max_runtime_is_stopped_not_destroyed(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project, max_runtime_minutes=30)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    runner_api.start(db_session, row, now=NOW + timedelta(minutes=1), settings=get_settings())
    for minute in range(2, 34):
        _beat(db_session, row, minute, sessions=1)
        _pass(db_session, client, NOW + timedelta(minutes=minute))
    assert row.stop_requested_at is not None
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=34))
    _pass(db_session, client, NOW + timedelta(minutes=35))

    assert row.state is ComputeInstanceState.STOPPED and job.state is ComputeJobState.STOPPED
    assert "max runtime" in job.state_reason
    assert row.provider_instance_id in fake.instances


def test_vasts_lag_after_a_stop_is_not_a_member_restart(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    fake.lag_stops = True
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))  # Krater stops it; Vast still says running
    before = row.accrued_microusd

    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert row.state is ComputeInstanceState.STOPPED
    assert "unexpected_start" not in _events(db_session, job)
    assert row.accrued_microusd - before < row.gpu_microusd / 60  # disk only


def test_a_failing_ssh_key_check_refuses_boxes_but_not_jobs(db_session, member, project, fake) -> None:  # noqa: F811
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/ssh/":
            return httpx.Response(502, text="bad gateway")
        return fake.handle(request)

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    box = _box(db_session, member, project)
    job = _submit(db_session, member, project, now=NOW + timedelta(seconds=1))

    _pass(db_session, client, NOW + timedelta(seconds=2))

    assert box.state is ComputeJobState.QUEUED
    assert job.state is ComputeJobState.STARTING


def _notices(db_session, action):
    from krater.models import AuditEvent

    return db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == action)).all()


def test_an_idle_stop_and_the_destroy_are_noticed_for_slack_but_a_members_stop_is_not(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    for minute in range(3, 35):
        _beat(db_session, row, minute)
        _pass(db_session, client, NOW + timedelta(minutes=minute))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=34, seconds=30))
    _pass(db_session, client, NOW + timedelta(minutes=35))

    (stopped,) = _notices(db_session, compute_jobs.AUDIT_BOX_STOPPED)
    assert stopped.payload["job_name"] == job.name and stopped.payload["synced"] is True
    assert "idle" in stopped.payload["reason"]

    _pass(db_session, client, NOW + timedelta(minutes=35, hours=23, seconds=30))
    (soon,) = _notices(db_session, compute_jobs.AUDIT_BOX_DESTROY_SOON)
    assert soon.payload["destroy_at"].startswith("2026")
    _pass(db_session, client, NOW + timedelta(minutes=36, hours=24))
    assert len(_notices(db_session, compute_jobs.AUDIT_BOX_DESTROYED)) == 1


def test_a_box_the_member_stops_gets_no_notice(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    compute_jobs.stop_box(db_session, member, job=job, now=NOW + timedelta(minutes=3))
    runner_api.synced(db_session, row, now=NOW + timedelta(minutes=3, seconds=10))
    _pass(db_session, client, NOW + timedelta(minutes=4))

    assert job.state is ComputeJobState.STOPPED
    assert _notices(db_session, compute_jobs.AUDIT_BOX_STOPPED) == []
