"""Data transfer and the image pull in the live spend estimate, the budget held for them, the credit reserve, and the
market data Krater collects for later price work (waits, outbids, hosts' prices)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from krater.compute import offers, reconcile, runner_api, scheduler, spend
from krater.models import (
    ComputeEscalation,
    ComputeInstanceState,
    ComputeJobState,
    ComputeSettings,
    ComputeWaitSample,
    GpuOfferObservation,
    ImageSize,
    ProviderAccount,
)
from krater.registry import FakeRegistry, ImageDetails
from krater.services import compute_halt, price_history
from krater.vast import Offer, usd_to_microusd
from tests.compute.conftest import current_weave
from tests.compute.test_scheduler import NOW, RUNNER_SHA, SETTINGS, _instance, _runner_starts, _submit

GB = 1_000_000_000
IMAGE = "ghcr.io/alice/train:v3"
DIGEST = "sha256:" + "e" * 64
# The fake's hosts charge $0.002/GB each way ($2/TB): 2,000 micro-dollars a GB.
PER_GB = 2_000


def _registry(size_bytes: int = 4 * GB) -> FakeRegistry:
    registry = FakeRegistry()
    registry.add(
        IMAGE,
        ImageDetails(
            digest=DIGEST, platform_digest=None, platform="linux/amd64", created=None, size_bytes=size_bytes, layers=3
        ),
    )
    return registry


def _pass(db_session, client, now=NOW, registry=None):
    return scheduler.run_pass(
        db_session,
        client,
        SETTINGS,
        now=now,
        runner_sha256=RUNNER_SHA,
        weave_client=current_weave[0],
        registry=registry,
    )


def _running(db_session, member, project, client, fake, *, registry=None, **overrides):  # noqa: F811
    job = _submit(db_session, member, project, image=IMAGE, **overrides)
    _pass(db_session, client, NOW, registry)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=1))
    _pass(db_session, client, NOW + timedelta(minutes=1), registry)
    assert job.state is ComputeJobState.RUNNING
    return job, row


def _beat(db_session, row, minute, **beat):
    return runner_api.heartbeat(db_session, row, {"phase": "running", **beat}, now=NOW + timedelta(minutes=minute))


# -- Network use from the runner -----------------------------------------------------------------------------------


def test_the_runners_byte_counts_are_priced_into_the_estimate(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running(db_session, member, project, client, fake)
    before = row.accrued_microusd

    _beat(db_session, row, 2, net_in_bytes=3 * GB, net_out_bytes=GB)
    _pass(db_session, client, NOW + timedelta(minutes=2))

    assert row.net_in_bytes == 3 * GB and row.net_out_bytes == GB
    assert row.est_transfer_in_microusd == 3 * PER_GB
    assert row.est_transfer_out_microusd == PER_GB
    time_part = row.est_gpu_microusd + row.est_disk_microusd
    assert row.accrued_microusd == time_part + 4 * PER_GB
    assert row.accrued_microusd > before
    assert job.spend_cents == spend.ceil_div(row.accrued_microusd, spend.MICROUSD_PER_CENT)


def test_only_new_bytes_are_priced_each_pass(db_session, member, project, client, fake) -> None:  # noqa: F811
    _, row = _running(db_session, member, project, client, fake)
    _beat(db_session, row, 2, net_in_bytes=GB, net_out_bytes=0)
    _pass(db_session, client, NOW + timedelta(minutes=2))
    _beat(db_session, row, 3, net_in_bytes=GB, net_out_bytes=0)  # nothing new
    _pass(db_session, client, NOW + timedelta(minutes=3))
    _beat(db_session, row, 4, net_in_bytes=2 * GB, net_out_bytes=0)
    _pass(db_session, client, NOW + timedelta(minutes=4))

    assert row.est_transfer_in_microusd == 2 * PER_GB
    assert row.net_priced_in_bytes == 2 * GB


def test_a_report_can_only_raise_the_count_and_only_as_fast_as_the_link(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    _, row = _running(db_session, member, project, client, fake)
    assert row.link_down_mbps == 800 and row.link_up_mbps == 400
    _beat(db_session, row, 2, net_in_bytes=GB, net_out_bytes=GB)

    # A minute later the runner claims a petabyte: at most 800 Mbps x 2 minutes x 1.5 more is believed.
    _beat(db_session, row, 3, net_in_bytes=10**15, net_out_bytes=0)

    allowance = int(800 * 1_000_000 / 8 * runner_api.MIN_REPORT_SECONDS * runner_api.LINK_SLACK)
    assert row.net_in_bytes == GB + allowance
    assert row.net_out_bytes == GB  # never lowered


def test_a_runner_that_sends_no_counts_changes_nothing(db_session, member, project, client, fake) -> None:  # noqa: F811
    _, row = _running(db_session, member, project, client, fake)
    _beat(db_session, row, 2, gpu_util=90)
    _pass(db_session, client, NOW + timedelta(minutes=2))
    assert row.net_in_bytes == 0 and row.net_reported_at is None
    assert row.est_transfer_in_microusd == 0


def test_after_reconciliation_bytes_are_not_counted_twice(db_session, member, project, client, fake) -> None:  # noqa: F811
    _, row = _running(db_session, member, project, client, fake)
    _beat(db_session, row, 2, net_in_bytes=GB, net_out_bytes=0)
    _pass(db_session, client, NOW + timedelta(minutes=2))
    # Vast charged more than Krater estimated, its figure including the data so far: it replaces the estimate.
    fake.add_charge(row.provider_instance_id, gpu="0.50", disk="0.01", data_in="0.002")
    reconcile.run(db_session, client, SETTINGS, now=NOW + timedelta(minutes=3))
    assert row.accrued_microusd == usd_to_microusd(Decimal("0.512"))
    assert row.charged_items == {"gpu": 500_000, "disk": 10_000, "bwd": 2_000, "bwu": 0}

    _beat(db_session, row, 4, net_in_bytes=GB, net_out_bytes=0)  # no new bytes since
    _pass(db_session, client, NOW + timedelta(minutes=4))
    assert row.est_transfer_in_microusd == PER_GB  # still just the one GB
    assert row.accrued_microusd - usd_to_microusd(Decimal("0.512")) < 10_000  # only a minute of time added


# -- The image pull ------------------------------------------------------------------------------------------------


def test_the_image_pull_is_priced_once_the_provider_has_the_machine(db_session, member, project, client, fake) -> None:  # noqa: F811
    registry = _registry(4 * GB)
    job = _submit(db_session, member, project, image=IMAGE)
    _pass(db_session, client, NOW, registry)
    row = _instance(db_session, job)
    assert row.image_bytes == 4 * GB
    assert row.est_image_microusd == 0  # not at the create, which might never happen

    fake.set_status(row.provider_instance_id, "loading")
    _pass(db_session, client, NOW + timedelta(minutes=1), registry)
    _pass(db_session, client, NOW + timedelta(minutes=2), registry)

    assert row.est_image_microusd == 4 * PER_GB
    assert db_session.get(ImageSize, DIGEST).size_bytes == 4 * GB
    assert registry.calls.count(("size", f"ghcr.io/alice/train@{DIGEST}")) == 1  # cached by digest


def test_a_registry_that_cant_say_the_size_leaves_the_pull_out(db_session, member, project, client, fake) -> None:  # noqa: F811
    registry = _registry()
    registry.fail = "unreachable"
    job = _submit(db_session, member, project, image=IMAGE)
    _pass(db_session, client, NOW, registry)
    row = _instance(db_session, job)
    assert row.image_bytes is None and row.est_image_microusd == 0


def test_a_machine_that_never_appeared_is_not_charged_for_a_pull(db_session, member, project, client, fake) -> None:  # noqa: F811
    registry = _registry(4 * GB)
    job = _submit(db_session, member, project, image=IMAGE)
    _pass(db_session, client, NOW, registry)
    row = _instance(db_session, job)
    row.state = ComputeInstanceState.CREATE_FAILED
    db_session.flush()
    spend.accrue(db_session, now=NOW + timedelta(minutes=10))
    assert row.est_image_microusd == 0


# -- Choosing a host -----------------------------------------------------------------------------------------------


def _offer(offer_id: str, *, down_mbps: float | None, min_bid: str = "0.10") -> Offer:
    return Offer.from_json(
        {
            "id": int(offer_id),
            "machine_id": int(offer_id) + 1000,
            "gpu_name": "RTX 4090",
            "num_gpus": 1,
            "dph_base": 0.4,
            "dph_total": 0.41,
            "min_bid": float(min_bid),
            "storage_cost": 0.1,
            "inet_up_cost": 0.002,
            "inet_down_cost": 0.002,
            "inet_down": down_mbps,
            "reliability": 0.99,
        }
    )


def test_a_slow_link_loses_to_a_slightly_dearer_fast_one_for_a_big_image() -> None:
    slow = _offer("1", down_mbps=50, min_bid="0.100")
    fast = _offer("2", down_mbps=2000, min_bid="0.102")
    common = {
        "rental_type": "bid",
        "disk_gb": 32,
        "margin_percent": 0,
        "limit_microusd": 10**6,
        "max_microusd": 10**6,
        "several_models": False,
    }
    # No image size known: the cheaper one wins.
    assert offers.choose([slow, fast], **common).offer.id == "1"
    # A 20 GB image: 53 minutes at 50 Mbps against 80 seconds; over a 2-hour job that outweighs $0.002/h.
    transfer = offers.Transfer(runtime_hours=Decimal(2), image_bytes=20 * GB)
    assert offers.choose([slow, fast], transfer=transfer, **common).offer.id == "2"


def test_pull_time_assumes_a_modest_link_when_the_offer_doesnt_say() -> None:
    assert offers.pull_hours(GB, None) == offers.pull_hours(GB, offers.DEFAULT_DOWN_MBPS)
    assert offers.pull_hours(0, 10) == 0


# -- Budget held for data ------------------------------------------------------------------------------------------


def test_the_budget_must_cover_the_pull_and_expected_data_in_too(
    db_session, member, project, client, fake, monkeypatch
) -> None:  # noqa: F811
    # One hour of the cheapest 3060 is 7 cents; 200 GB in at $2/TB is 40 cents more.
    job = _submit(db_session, member, project, data_in_gb=200)
    monkeypatch.setattr("krater.compute.scheduler.budget.remaining_cents", lambda session, project: 20)
    _pass(db_session, client)
    assert job.state is ComputeJobState.QUEUED
    assert "of data" in job.state_reason and job.state_reason.startswith("Waiting for budget")


def test_the_hold_includes_the_expected_data(db_session, member, project, client, fake) -> None:  # noqa: F811
    plain = _submit(db_session, member, project, max_runtime_minutes=60)
    with_data = _submit(
        db_session,
        member,
        project,
        max_runtime_minutes=60,
        data_out_gb=100,
        now=NOW + timedelta(seconds=1),
    )
    _pass(db_session, client)
    # 100 GB out at $2/TB is 20 cents more held.
    assert with_data.hold_cents - plain.hold_cents >= 19


# -- Credit reserve ------------------------------------------------------------------------------------------------


def _credit(db_session, usd: str, *, at=NOW) -> None:
    db_session.add(ProviderAccount(provider="vast", credit_microusd=usd_to_microusd(Decimal(usd)), observed_at=at))
    db_session.flush()


def _reserve(db_session, hours: int) -> None:
    compute_halt.current(db_session)
    db_session.get(ComputeSettings, 1).credit_reserve_hours = hours
    db_session.flush()


def test_the_credit_reserve_is_off_by_default(db_session, member, project, client, fake) -> None:  # noqa: F811
    _credit(db_session, "0.01")
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    assert job.state is ComputeJobState.STARTING


def test_with_a_reserve_low_credit_holds_new_rentals(db_session, member, project, client, fake) -> None:  # noqa: F811
    _reserve(db_session, 24)
    _credit(db_session, "1.00")  # 24 h of a $0.064/h machine is $1.53
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    assert job.state is ComputeJobState.QUEUED
    assert "credit" in job.state_reason and "add credit" in job.state_reason


def test_a_stale_or_missing_credit_reading_never_holds_anything(db_session, member, project, client, fake) -> None:  # noqa: F811
    _reserve(db_session, 24)
    _credit(db_session, "1.00", at=NOW - timedelta(hours=3))
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    assert job.state is ComputeJobState.STARTING


# -- Data for later: waits, outbids, hosts' prices -----------------------------------------------------------------


def test_a_rental_records_how_long_the_job_waited_by_the_wall_clock(
    db_session, member, admin, project, client, fake
) -> None:  # noqa: F811
    job = _submit(db_session, member, project, max_wait_minutes=600)
    compute_halt.pause(db_session, admin, reason="r", now=NOW)
    _pass(db_session, client, NOW)
    compute_halt.resume(db_session, admin, reason="r", now=NOW + timedelta(hours=2))
    _pass(db_session, client, NOW + timedelta(hours=2, minutes=1))

    sample = db_session.scalar(sa.select(ComputeWaitSample).where(ComputeWaitSample.job_id == job.id))
    assert sample.outcome == "rented"
    # The halt moved queued_at forward, but the wait sample counts the real two hours.
    assert sample.waited_seconds == 2 * 3600 + 60
    assert sample.rented_microusd == _instance(db_session, job).full_microusd
    assert sample.rental_type == "bid" and sample.gpu_names == ["RTX 3060"]


def test_a_job_that_gives_up_records_its_wait(db_session, member, project, client, fake) -> None:  # noqa: F811
    fake.offers.clear()
    job = _submit(db_session, member, project, max_wait_minutes=30, on_max_wait=ComputeEscalation.CANCEL)
    _pass(db_session, client, NOW + timedelta(minutes=31))
    assert job.state is ComputeJobState.CANCELLED
    sample = db_session.scalar(sa.select(ComputeWaitSample).where(ComputeWaitSample.job_id == job.id))
    assert sample.outcome == "cancelled" and sample.waited_seconds == 31 * 60


def test_a_machine_records_the_market_price_and_its_outbids(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running(db_session, member, project, client, fake)
    assert row.market_gpu_microusd == 50_000  # the offer's minimum bid; Krater bid a margin over it
    assert row.gpu_microusd > row.market_gpu_microusd

    fake.set_status(row.provider_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=2))
    assert row.state is ComputeInstanceState.PAUSED and row.outbid_count == 1


def test_price_samples_keep_the_cheapest_hosts_and_prune_old_ones(db_session, client, fake) -> None:  # noqa: F811
    price_history.record(db_session, client, gpu_names=["RTX 3060"], disk_gb=32, now=NOW)
    rows = list(db_session.scalars(sa.select(GpuOfferObservation).order_by(GpuOfferObservation.full_microusd)))
    assert rows and {r.rental_type for r in rows} == {"bid", "ondemand"}
    assert all(r.gpu_name == "RTX 3060" and r.machine_id for r in rows)

    later = NOW + timedelta(days=price_history.OBSERVATION_DAYS + 1)
    assert price_history.prune_observations(db_session, now=later) == len(rows)
