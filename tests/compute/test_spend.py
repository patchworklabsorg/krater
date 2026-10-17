"""Krater Compute's live spend estimate, budget warnings and the hard stop at the ceiling."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from krater.compute import scheduler, spend
from krater.models import (
    AuditEvent,
    ComputeInstanceState,
    ComputeJobState,
    QuiltOutbox,
    ReviewDecision,
    SpendSnapshot,
    SpendSource,
)
from krater.services import budget, budget_alerts, projects
from tests.compute.test_scheduler import NOW, SETTINGS, _beat, _instance, _pass, _runner_starts, _submit
from tests.factories import new_project


def _running(db_session, member, project, client, fake, **overrides):  # noqa: F811
    job = _submit(db_session, member, project, **overrides)
    _pass(db_session, client)  # rented at NOW
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=4))  # pulled for 4 minutes: disk only
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=4))
    return job, row


def test_disk_bills_from_create_and_the_gpu_only_while_running(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running(db_session, member, project, client, fake)
    assert row.accrued_microusd == round(row.disk_microusd * 4 / 60)

    _beat(db_session, row, NOW + timedelta(minutes=63))
    _pass(db_session, client, NOW + timedelta(minutes=64))  # then an hour running

    assert row.accrued_microusd == round(row.disk_microusd * 4 / 60) + row.full_microusd
    assert job.spend_cents == 7  # ceil($0.0644 accrued)


def test_paused_time_bills_disk_only(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running(db_session, member, project, client, fake)
    _pass(db_session, client, NOW + timedelta(minutes=5))
    fake.set_status(row.provider_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))  # running until here, paused from here
    before = row.accrued_microusd

    _pass(db_session, client, NOW + timedelta(minutes=8))

    assert row.state is ComputeInstanceState.PAUSED
    assert row.accrued_microusd - before == round(row.disk_microusd * 2 / 60)


def test_spend_becomes_a_vast_estimate_snapshot_and_a_quilt_event(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running(db_session, member, project, client, fake)

    _pass(db_session, client, NOW + timedelta(hours=10))

    snapshot = db_session.scalars(
        sa.select(SpendSnapshot).where(SpendSnapshot.project_id == project.id).order_by(SpendSnapshot.taken_at.desc())
    ).first()
    assert snapshot.source is SpendSource.VAST_ESTIMATE
    assert snapshot.estimated_spend_cents == job.spend_cents
    assert budget.latest_spend_cents(db_session, project) == job.spend_cents
    events = db_session.scalars(
        sa.select(QuiltOutbox).where(QuiltOutbox.external_id == str(project.id), QuiltOutbox.type == "spend.recorded")
    ).all()
    assert events and events[-1].payload["spent_cents_total"] == job.spend_cents


@pytest.fixture
def small_project(db_session, member, admin, weave):  # noqa: F811
    project = new_project(db_session, member, title="Small", write_up="w", budget_requested_cents=100)
    projects.submit(db_session, member, project=project)
    projects.admin_decide(
        db_session,
        admin,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        reason="ok",
        weave_client=weave,
    )
    db_session.refresh(project)
    return project


def test_the_project_is_warned_then_stopped_before_it_overruns(db_session, member, small_project, client, fake) -> None:  # noqa: F811
    # An RTX 4090 bid at about $0.233/h: the $1.00 budget lasts about 4 h 17 min.
    job, row = _running(
        db_session,
        member,
        small_project,
        client,
        fake,
        gpu_names=("RTX 4090",),
        target_microusd=300_000,
        max_microusd=400_000,
    )
    assert job.hold_cents == 100  # all that was left, since 8 h would cost more

    minute = 5
    while job.state is not ComputeJobState.FAILED and minute < 6 * 60:
        minute += 1
        _beat(db_session, row, NOW + timedelta(minutes=minute))
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert job.state is ComputeJobState.FAILED
    assert "budget" in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}
    assert budget.latest_spend_cents(db_session, small_project) <= 100
    actions = list(
        db_session.scalars(
            sa.select(AuditEvent.action)
            .where(AuditEvent.project_id == small_project.id)
            .order_by(AuditEvent.created_at)
        )
    )
    assert actions.count(budget_alerts.AUDIT_BUDGET_WARNING) == 1
    assert actions.count(budget_alerts.AUDIT_BUDGET_TEARDOWN) == 1


def test_an_over_budget_project_starts_nothing_new(db_session, member, small_project, client, fake) -> None:  # noqa: F811
    db_session.add(
        SpendSnapshot(
            project_id=small_project.id, estimated_spend_cents=100, source=SpendSource.VAST_ESTIMATE, taken_at=NOW
        )
    )
    db_session.flush()
    job = _submit(db_session, member, small_project)

    _pass(db_session, client, NOW + timedelta(minutes=1))

    assert job.state is ComputeJobState.QUEUED
    assert job.state_reason.startswith("Waiting for budget")
    assert fake.instances == {}


def test_next_minute_counts_running_gpu_and_paused_disk(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running(db_session, member, project, client, fake)
    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert spend.next_minute_cents(db_session, project) == 1  # ceil($0.0638 / 60)
    assert scheduler.MICROUSD_PER_CENT == spend.MICROUSD_PER_CENT
    assert SETTINGS.budget_warn_percent == 80
