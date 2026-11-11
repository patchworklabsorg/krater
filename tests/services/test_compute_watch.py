"""What the worker watches for admins (`krater.services.compute_watch`): storage use by project, the provider's credit,
deleting a finished project's files on its date, and the alerts and channel notices that go with them."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import (
    AuditEvent,
    ComputeEscalation,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    ComputeSettings,
    StorageUsage,
)
from krater.services import compute_halt, compute_watch, ops_alerts, projects, slack_notify
from krater.services.actor import Actor
from krater.services.compute_jobs import AUDIT_JOB_ENDED, AUDIT_JOB_IDLE
from krater.slack.fake import FakeSlackClient
from krater.storage import FakeObjectStore
from tests.factories import new_project

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
CHANNEL = "C_ALERTS"


def _put(store: FakeObjectStore, key: str, size: int) -> None:
    store.put(key, content_type="application/octet-stream", size_bytes=size)


def _job(db_session: Session, project, member: Actor) -> ComputeJob:
    job = ComputeJob(
        project_id=project.id,
        submitter_id=member.user.id,
        kind=ComputeJobKind.JOB,
        name="train",
        image="ghcr.io/a/b:1",
        command="python t.py",
        gpu_names=["RTX 3060"],
        num_gpus=1,
        disk_gb=32,
        target_microusd=50_000,
        max_microusd=60_000,
        interruptible=True,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.KEEP_WAITING,
        max_runtime_minutes=60,
        state=ComputeJobState.SUCCEEDED,
        queued_at=NOW,
    )
    db_session.add(job)
    db_session.flush()
    return job


def _settings_row(db_session: Session) -> ComputeSettings:
    compute_halt.current(db_session)
    return db_session.get(ComputeSettings, 1)


# -- Storage use ---------------------------------------------------------------------------------------------------


def test_storage_is_counted_by_project_with_images_apart(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover")
    compute, images = FakeObjectStore(), FakeObjectStore()
    _put(compute, f"projects/{project.id}/jobs/{uuid.uuid4()}/checkpoints/a.ckpt", 300)
    _put(compute, f"projects/{project.id}/jobs/{uuid.uuid4()}/outputs/b.txt", 200)
    _put(compute, f"projects/{uuid.uuid4()}/jobs/x/outputs/gone.txt", 7)  # a project Krater doesn't know
    _put(images, "revisions/r/screenshot.png", 40)

    total = compute_watch.record_storage(db_session, compute, images, now=NOW)

    assert total == 547
    assert compute_watch.storage_by_project(db_session) == {project.id: 500}
    assert compute_watch.storage_totals(db_session) == (547, 40, NOW)

    compute_watch.record_storage(db_session, FakeObjectStore(), None, now=NOW + timedelta(hours=1))
    assert db_session.scalars(sa.select(StorageUsage)).all() == []  # replaced, not added to


# -- Finished projects' files --------------------------------------------------------------------------------------


def _withdrawn(db_session: Session, member: Actor):
    project = new_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    projects.submit(db_session, member, project=project, criteria_read=True)
    projects.withdraw(db_session, member, project=project, reason="done")
    return project


def test_finishing_a_project_fixes_its_file_deletion_date(db_session: Session, member: Actor) -> None:
    project = _withdrawn(db_session, member)

    assert project.files_delete_on == project.finished_at + timedelta(days=30)


def test_with_the_setting_at_zero_files_are_kept(db_session: Session, member: Actor) -> None:
    _settings_row(db_session).finished_files_days = 0
    project = _withdrawn(db_session, member)

    assert project.files_delete_on is None


def test_files_are_deleted_on_the_date_and_not_before(db_session: Session, member: Actor) -> None:
    project = _withdrawn(db_session, member)
    other = new_project(db_session, member, title="Still going")
    store = FakeObjectStore()
    _put(store, f"projects/{project.id}/jobs/j/outputs/a.txt", 10)
    _put(store, f"projects/{project.id}/jobs/j/checkpoints/b.ckpt", 20)
    _put(store, f"projects/{other.id}/jobs/k/outputs/keep.txt", 5)
    due = project.files_delete_on

    assert compute_watch.delete_due_files(db_session, store, now=due - timedelta(minutes=1)) == []
    assert len(store.list_prefix(f"projects/{project.id}/")) == 2

    assert compute_watch.delete_due_files(db_session, store, now=due) == [project.id]
    assert store.list_prefix(f"projects/{project.id}/") == []
    assert len(store.list_prefix(f"projects/{other.id}/")) == 1
    assert project.files_deleted_at == due
    (event,) = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == compute_watch.AUDIT_FILES_DELETED))
    assert event.payload["objects"] == 2 and event.payload["bytes"] == 30

    # Once only.
    assert compute_watch.delete_due_files(db_session, store, now=due + timedelta(days=1)) == []


def test_a_project_that_finished_before_dates_existed_is_never_deleted(db_session: Session, member: Actor) -> None:
    project = _withdrawn(db_session, member)
    project.files_delete_on = None
    store = FakeObjectStore()
    _put(store, f"projects/{project.id}/jobs/j/outputs/a.txt", 10)

    assert compute_watch.delete_due_files(db_session, store, now=NOW + timedelta(days=3650)) == []
    assert len(store.list_prefix(f"projects/{project.id}/")) == 1


def test_the_finish_notice_gives_the_file_deletion_date_when_the_project_ran_compute(
    db_session: Session, member: Actor
) -> None:
    project = _withdrawn(db_session, member)
    _job(db_session, project, member)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover")

    slack_notify.announce_archive(db_session, slack_client, project=project)

    (message,) = slack_client.messages.values()
    due = project.files_delete_on
    assert f"deletes the project's checkpoints and outputs on {due:%B} {due.day}, {due:%Y}" in message["text"]
    assert "krater download" in message["text"]


def test_the_finish_notice_says_nothing_about_files_without_compute(db_session: Session, member: Actor) -> None:
    project = _withdrawn(db_session, member)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover")

    slack_notify.announce_archive(db_session, slack_client, project=project)

    (message,) = slack_client.messages.values()
    assert "checkpoints" not in message["text"]


# -- Alerts --------------------------------------------------------------------------------------------------------


def _watch(db_session: Session, slack: FakeSlackClient, now: datetime, settings: Settings | None = None) -> list[str]:
    ops_alerts.check(db_session, settings or Settings(), now=now)
    return ops_alerts.deliver(db_session, slack, CHANNEL, now=now)


def _last(slack: FakeSlackClient) -> str:
    """The alerts channel's newest message that isn't about GPU prices (the tests don't refresh them)."""
    ordered = sorted(slack.messages.items(), key=lambda kv: kv[0][1])
    texts = [m["text"] for (channel, _), m in ordered if channel == CHANNEL and "GPU prices" not in m["text"]]
    return texts[-1]


def test_stopping_compute_opens_an_alert_until_it_is_back_on(db_session: Session, admin: Actor) -> None:
    slack = FakeSlackClient()
    compute_halt.pause(db_session, admin, reason="Vast is billing oddly", now=NOW)

    assert "compute_halted" in _watch(db_session, slack, NOW)
    assert "Vast is billing oddly" in _last(slack)

    compute_halt.resume(db_session, admin, reason="fixed", now=NOW + timedelta(minutes=10))
    assert "compute_halted" in _watch(db_session, slack, NOW + timedelta(minutes=10))
    assert "cleared" in _last(slack)


def test_unknown_charges_alert_for_a_day(db_session: Session) -> None:
    slack = FakeSlackClient()
    db_session.add(
        AuditEvent(action=ops_alerts.AUDIT_UNKNOWN_CHARGES, payload={"sources": ["instance-77"]}, created_at=NOW)
    )
    db_session.flush()

    assert "unknown_charges" in _watch(db_session, slack, NOW + timedelta(hours=1))
    assert "instance-77" in _last(slack)
    assert "unknown_charges" in _watch(db_session, slack, NOW + timedelta(hours=25))
    assert "cleared" in _last(slack)


def test_old_unknown_charges_dont_alert(db_session: Session) -> None:
    db_session.add(
        AuditEvent(
            action=ops_alerts.AUDIT_UNKNOWN_CHARGES,
            payload={"sources": ["instance-1"]},
            created_at=NOW - timedelta(days=3),
        )
    )
    db_session.flush()

    assert "unknown_charges" not in _watch(db_session, FakeSlackClient(), NOW)


def test_low_credit_alerts_only_once_a_threshold_is_set(db_session: Session) -> None:
    slack = FakeSlackClient()
    compute_watch.record_credit(db_session, "vast", Decimal("12.50"), now=NOW)
    assert "low_credit" not in _watch(db_session, slack, NOW)

    _settings_row(db_session).credit_alert_cents = 5000
    assert "low_credit" in _watch(db_session, slack, NOW + timedelta(minutes=5))
    assert "$12.50 left" in _last(slack)

    compute_watch.record_credit(db_session, "vast", Decimal("80"), now=NOW + timedelta(hours=1))
    assert "low_credit" in _watch(db_session, slack, NOW + timedelta(hours=1))
    assert "cleared" in _last(slack)


def test_a_missing_credit_reading_keeps_the_last_one(db_session: Session) -> None:
    compute_watch.record_credit(db_session, "vast", Decimal("40"), now=NOW)
    compute_watch.record_credit(db_session, "vast", None, now=NOW + timedelta(hours=1))

    latest = compute_watch.latest_credit(db_session)
    assert (latest.credit_microusd, latest.observed_at) == (40_000_000, NOW)


def test_storage_nearly_full_alerts_when_the_capacity_is_known(db_session: Session) -> None:
    slack = FakeSlackClient()
    store = FakeObjectStore()
    _put(store, "projects/x/a", 900)
    compute_watch.record_storage(db_session, store, None, now=NOW)

    assert "store_full" not in _watch(db_session, slack, NOW)  # capacity unknown
    tiny = Settings(compute_store_capacity_gb=1)
    db_session.execute(sa.update(StorageUsage).values(bytes=int(0.85 * 1024**3)))
    assert "store_full" in _watch(db_session, slack, NOW, tiny)
    assert "of 1 GB" in _last(slack)


# -- Channel notices -----------------------------------------------------------------------------------------------


def _notice(db_session: Session, project, member: Actor, action: str, **payload) -> None:
    db_session.add(
        AuditEvent(
            action=action,
            project_id=project.id,
            payload={"job_id": "j1", "job_name": "train", "owner_id": str(member.user.id), **payload},
        )
    )
    db_session.flush()


def test_job_ended_and_idle_notices_mention_whoever_submitted_it(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover")
    member.user.slack_user_id = "U_OWNER"
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover")
    _notice(db_session, project, member, AUDIT_JOB_ENDED, state="failed", reason="Exited with 1.", spend_cents=125)
    _notice(db_session, project, member, AUDIT_JOB_IDLE, minutes=30, gpu_util=0, hourly_microusd=250_000)

    slack_notify.sync_box_notifications(db_session, slack_client)
    slack_notify.sync_box_notifications(db_session, slack_client)

    texts = sorted(m["blocks"][0]["text"]["text"] for m in slack_client.messages.values())
    assert len(texts) == 2
    failed = next(t for t in texts if "Job failed" in t)
    assert "<@U_OWNER>" in failed and "Exited with 1." in failed and "$1.25" in failed
    idle = next(t for t in texts if "GPU idle" in t)
    assert "30 minutes" in idle and "$0.25/h" in idle


def test_a_succeeded_job_notice_leaves_out_the_reason(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover")
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover")
    _notice(db_session, project, member, AUDIT_JOB_ENDED, state="succeeded", reason="Exited with 0.", spend_cents=9)

    slack_notify.sync_box_notifications(db_session, slack_client)

    (message,) = slack_client.messages.values()
    text = message["blocks"][0]["text"]["text"]
    assert "Job finished" in text and "Exited with 0" not in text and "Job owner" in text


def _messaged(db_session: Session, member: Actor, setting: int, *states: str, slack=None, at=None):
    """Job-ended notices for `states`, synced to the submitter with `dm_job_endings` at `setting`."""
    project = new_project(db_session, member, title="Rover")
    member.user.slack_user_id = "U_OWNER"
    _settings_row(db_session).dm_job_endings = setting
    for state in states:
        db_session.add(
            AuditEvent(
                action=AUDIT_JOB_ENDED,
                project_id=project.id,
                created_at=at or datetime.now(UTC),
                payload={
                    "job_id": "j1",
                    "job_name": f"job-{state}",
                    "owner_id": str(member.user.id),
                    "state": state,
                    "reason": "Exited with 1.",
                    "spend_cents": 10,
                },
            )
        )
    db_session.flush()
    slack = slack or FakeSlackClient()
    slack_notify.sync_job_ending_messages(db_session, slack)
    return slack


def _dms(slack: FakeSlackClient) -> list[str]:
    return sorted(m["text"] for (channel, _), m in slack.messages.items() if channel == "U_OWNER")


def test_submitters_arent_messaged_by_default(db_session: Session, member: Actor) -> None:
    assert _dms(_messaged(db_session, member, 0, "failed", "succeeded")) == []


def test_failures_only_messages_the_submitter_once_about_a_failure(db_session: Session, member: Actor) -> None:
    slack = _messaged(db_session, member, 1, "failed", "succeeded")
    slack_notify.sync_job_ending_messages(db_session, slack)

    dms = _dms(slack)
    assert len(dms) == 1 and "Job failed" in dms[0] and "job-failed" in dms[0]


def test_always_messages_every_ending(db_session: Session, member: Actor) -> None:
    dms = _dms(_messaged(db_session, member, 2, "failed", "succeeded", "cancelled"))
    assert len(dms) == 3


def test_jobs_that_ended_before_the_window_arent_messaged(db_session: Session, member: Actor) -> None:
    old = datetime.now(UTC) - slack_notify.DIRECT_MESSAGE_WINDOW - timedelta(minutes=1)
    assert _dms(_messaged(db_session, member, 2, "failed", at=old)) == []


def test_a_message_slack_refuses_is_tried_again(db_session: Session, member: Actor, monkeypatch) -> None:
    from krater.slack.errors import SlackRequestFailedError

    slack = FakeSlackClient()
    real = slack.post_message

    def refuse(channel_id, **kwargs):
        raise SlackRequestFailedError("messages_tab_disabled")

    monkeypatch.setattr(slack, "post_message", refuse)
    _messaged(db_session, member, 2, "failed", slack=slack)
    monkeypatch.setattr(slack, "post_message", real)
    slack_notify.sync_job_ending_messages(db_session, slack)

    assert len(_dms(slack)) == 1
