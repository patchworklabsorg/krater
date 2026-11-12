"""What a job may cost before it runs (`job_costs.estimate`) and what it cost, part by part (`job_costs.breakdown`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from krater.models import ComputeInstance, ComputeInstanceState, ComputeJob, GpuPriceSample
from krater.services import job_costs, slack_notify
from krater.services.actor import Actor
from krater.services.compute_jobs import AUDIT_JOB_ENDED, JobSpec
from krater.slack.fake import FakeSlackClient
from tests.factories import new_project
from tests.services.test_compute_watch import _notice

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
CAP = 20_000  # $20/TB


def _spec(**overrides) -> JobSpec:
    return JobSpec(
        **{
            "name": "train",
            "image": "ghcr.io/a/b:1",
            "target_microusd": 300_000,
            "max_microusd": 400_000,
            "gpu_names": ("RTX 4090",),
            "max_runtime_minutes": 600,
            **overrides,
        }
    )


def _sample(db_session, *, at, low, median, gpu="RTX 4090", rental="bid") -> None:
    db_session.add(
        GpuPriceSample(
            sampled_at=at,
            gpu_name=gpu,
            rental_type=rental,
            num_gpus=1,
            disk_gb=32,
            offer_count=40,
            min_microusd=low,
            p10_microusd=low,
            median_microusd=median,
        )
    )
    db_session.flush()


def test_the_estimate_is_an_upper_bound_at_the_max_runtime(db_session) -> None:
    estimate = job_costs.estimate(db_session, _spec(), transfer_cap_microusd_per_gb=CAP, now=NOW)

    assert estimate.up_to_cents == 400  # $0.40/h x 10 h
    assert estimate.data_cents == 0 and estimate.market is None
    assert "Up to $4.00 if it runs its full 10 hours" in estimate.summary()
    assert "finish sooner" in estimate.summary()


def test_the_members_data_estimate_is_counted_at_the_cap(db_session) -> None:
    estimate = job_costs.estimate(
        db_session, _spec(data_in_gb=500, data_out_gb=100), transfer_cap_microusd_per_gb=CAP, now=NOW
    )
    assert estimate.data_cents == 1200  # 600 GB at $20/TB
    assert estimate.total_up_to_cents == 1600


def test_recent_prices_come_from_the_last_day_of_history(db_session) -> None:
    _sample(db_session, at=NOW - timedelta(hours=1), low=280_000, median=350_000)
    _sample(db_session, at=NOW - timedelta(hours=2), low=300_000, median=370_000)
    _sample(db_session, at=NOW - timedelta(days=3), low=10_000, median=10_000)  # too old
    _sample(db_session, at=NOW - timedelta(hours=1), low=900_000, median=900_000, rental="ondemand")

    market = job_costs.estimate(db_session, _spec(), transfer_cap_microusd_per_gb=CAP, now=NOW).market

    assert market.low_microusd == 280_000 and market.typical_microusd == 360_000 and market.samples == 2


def test_two_gpus_cost_twice_and_a_market_above_the_max_means_waiting(db_session) -> None:
    _sample(db_session, at=NOW - timedelta(hours=1), low=250_000, median=300_000)

    estimate = job_costs.estimate(db_session, _spec(num_gpus=2), transfer_cap_microusd_per_gb=CAP, now=NOW)

    assert estimate.market.low_microusd == 500_000
    assert estimate.market_above_max and "expect it to wait" in estimate.summary()


def test_any_gpu_by_memory_has_no_market_price(db_session) -> None:
    _sample(db_session, at=NOW - timedelta(hours=1), low=250_000, median=300_000)
    spec = _spec(gpu_names=(), min_gpu_ram_gb=24)
    assert job_costs.estimate(db_session, spec, transfer_cap_microusd_per_gb=CAP, now=NOW).market is None


def _instance(attempt: int, **fields) -> ComputeInstance:
    return ComputeInstance(
        attempt=attempt,
        label=f"l{attempt}",
        state=ComputeInstanceState.DESTROYED,
        provider_offer_id="1",
        gpu_name="RTX 4090",
        interruptible=True,
        gpu_microusd=300_000,
        disk_microusd=5_000,
        **fields,
    )


def test_the_breakdown_puts_krater_and_vast_side_by_side() -> None:
    job = ComputeJob(name="train")
    job.instances = [
        _instance(
            1,
            accrued_microusd=1,
            est_gpu_microusd=600_000,
            est_disk_microusd=10_000,
            est_image_microusd=8_000,
            est_transfer_in_microusd=2_000,
            est_transfer_out_microusd=4_000,
            charged_items={"gpu": 610_000, "disk": 10_000, "bwd": 12_000, "bwu": 3_000, "storage": 500},
        ),
        _instance(2, accrued_microusd=1, est_gpu_microusd=100_000),
    ]

    costs = job_costs.breakdown(job)

    lines = {line.label: (line.estimate_microusd, line.charged_microusd) for line in costs.lines}
    assert lines["GPU"] == (700_000, 610_000)
    assert lines["Data in (and image pulls)"] == (10_000, 12_000)
    assert lines["Data out"] == (4_000, 3_000)
    assert lines["Other"] == (0, 500)
    assert costs.charged and costs.split_known
    assert costs.part_cents()["GPU"] == 61


def test_the_notices_parts_add_up_to_the_jobs_cost() -> None:
    """Staging, 2026-11-12: a $0.01 job read "GPU $0.01, disk $0.01, data $0.02": each part was rounded up alone."""
    job = ComputeJob(name="short")
    job.instances = [
        _instance(
            1,
            accrued_microusd=5_000,
            est_gpu_microusd=4_000,
            est_disk_microusd=300,
            est_image_microusd=1_000,
            est_transfer_out_microusd=6,
        )
    ]
    costs = job_costs.breakdown(job)

    assert costs.notice_cents(1) == {"gpu": 1, "disk": 0, "data": 0}
    parts = costs.notice_cents(350)
    assert sum(parts.values()) == 350 and parts["gpu"] > parts["data"] > parts["disk"]
    assert costs.notice_cents(0) == {"gpu": 0, "disk": 0, "data": 0}


def test_machines_from_before_the_split_say_it_isnt_known() -> None:
    job = ComputeJob(name="old")
    job.instances = [_instance(1, accrued_microusd=500_000)]
    costs = job_costs.breakdown(job)
    assert not costs.split_known and not costs.charged


def test_the_job_ended_notice_splits_the_cost(db_session: Session, member: Actor) -> None:
    project = new_project(db_session, member, title="Rover")
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover")
    _notice(
        db_session,
        project,
        member,
        AUDIT_JOB_ENDED,
        state="succeeded",
        spend_cents=350,
        cost_parts={"gpu": 300, "disk": 10, "data": 40},
    )

    slack_notify.sync_box_notifications(db_session, slack_client)

    (message,) = slack_client.messages.values()
    assert "It cost about $3.50 (GPU $3.00, disk $0.10, data $0.40)" in message["blocks"][0]["text"]["text"]
