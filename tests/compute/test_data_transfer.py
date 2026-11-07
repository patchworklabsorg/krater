"""Data transfer: Vast bills data into and out of a machine per GB, at the host's own price, on top of the hourly
price. Krater rents no host over the transfer price cap, ranks hosts by the member's own estimate, asks before queuing
a job whose transfer could cost a large share of the budget, and warns when Vast's transfer charges do."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from krater.compute import offers
from krater.models import AuditEvent, ComputeEvent, ComputeJobState
from krater.services import compute_jobs, compute_settings
from krater.services import projects as project_service
from krater.services.compute_jobs import AUDIT_TRANSFER_WARNING, JobSpec
from krater.services.errors import ConfirmationRequired, ValidationFailed
from krater.vast import Offer
from krater.vast.fake import _offer
from tests.compute.conftest import current_weave
from tests.compute.test_reconcile import _ran_an_hour, _reconcile
from tests.compute.test_scheduler import NOW, _instance, _pass, _submit

#: The default cap: $20 per TB each way.
CAP = 20_000


def _o(offer_id: int, *, min_bid: float, data_in: float = 0.002, data_out: float = 0.002) -> Offer:
    return Offer.from_json(
        _offer(
            offer_id,
            offer_id,
            "RTX 3060",
            dph_base=0.1,
            min_bid=min_bid,
            storage_cost=0.2,
            gpu_ram_mb=12288,
            inet_down_cost=data_in,
            inet_up_cost=data_out,
        )
    )


def _choose(found: list[Offer], transfer: offers.Transfer = offers.NO_TRANSFER) -> offers.Choice | None:
    return offers.choose(
        found,
        rental_type="bid",
        disk_gb=32,
        margin_percent=10,
        limit_microusd=1_000_000,
        max_microusd=1_000_000,
        several_models=False,
        transfer_cap_microusd_per_gb=CAP,
        transfer=transfer,
    )


# -- The cap --------------------------------------------------------------------------------------------


def test_the_search_asks_only_for_hosts_under_the_transfer_cap(db_session, member, project, client, fake) -> None:  # noqa: F811
    _submit(db_session, member, project)

    _pass(db_session, client)

    body = fake.search_bodies[0]
    assert body["inet_down_cost"] == {"lte": 0.02}
    assert body["inet_up_cost"] == {"lte": 0.02}


def test_a_host_over_the_cap_is_never_chosen_even_if_the_search_returns_it() -> None:
    cheap_hourly_dear_transfer = _o(1, min_bid=0.02, data_in=0.039)
    dearer_hourly = _o(2, min_bid=0.05)
    out_dear = _o(3, min_bid=0.01, data_out=0.08)

    choice = _choose([cheap_hourly_dear_transfer, out_dear, dearer_hourly])

    assert choice is not None and choice.offer.id == "2"
    assert (
        offers.price(
            cheap_hourly_dear_transfer,
            rental_type="bid",
            disk_gb=32,
            margin_percent=10,
            max_microusd=1_000_000,
            transfer_cap_microusd_per_gb=CAP,
        )
        is None
    )


def test_the_cheapest_price_a_waiting_member_is_told_leaves_out_hosts_over_the_cap() -> None:
    found = [_o(1, min_bid=0.02, data_in=0.039), _o(2, min_bid=0.05)]

    low = offers.cheapest(found, rental_type="bid", disk_gb=32, margin_percent=10, transfer_cap_microusd_per_gb=CAP)

    assert (
        low
        == offers.price(found[1], rental_type="bid", disk_gb=32, margin_percent=10, max_microusd=10**12).full_microusd
    )


def test_a_host_at_exactly_the_cap_can_be_rented() -> None:
    assert _choose([_o(1, min_bid=0.05, data_in=0.02, data_out=0.02)]) is not None


def test_the_scheduler_rents_under_the_cap_and_records_the_hosts_transfer_prices(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    # Cheaper by the hour than every default offer, but $50 per TB for data in.
    fake.add_offer(
        1010, 510, "RTX 3060", dph_base=0.03, min_bid=0.02, storage_cost=0.1, gpu_ram_mb=12288, inet_down_cost=0.05
    )
    job = _submit(db_session, member, project)

    _pass(db_session, client)

    row = _instance(db_session, job)
    assert row.provider_offer_id == "1001"
    assert (row.transfer_in_microusd_per_gb, row.transfer_out_microusd_per_gb) == (2_000, 2_000)


def test_an_admin_can_raise_one_projects_cap(db_session, member, admin, project, client, fake) -> None:  # noqa: F811
    fake.add_offer(
        1010, 510, "RTX 3060", dph_base=0.03, min_bid=0.02, storage_cost=0.1, gpu_ram_mb=12288, inet_down_cost=0.05
    )
    compute_settings.set_project_overrides(
        db_session,
        admin,
        project=project,
        overrides={"transfer_cap_microusd_per_gb": 60_000},
        reason="A data-heavy job.",
    )
    job = _submit(db_session, member, project)

    _pass(db_session, client)

    assert fake.search_bodies[0]["inet_down_cost"] == {"lte": 0.06}
    assert _instance(db_session, job).provider_offer_id == "1010"
    assert _instance(db_session, job).transfer_in_microusd_per_gb == 50_000


# -- Ranking by the member's estimate ---------------------------------------------------------------


def test_with_an_estimate_the_host_where_the_data_costs_less_wins() -> None:
    cheap_hourly = _o(1, min_bid=0.05, data_in=0.02)  # $20 per TB in
    cheap_transfer = _o(2, min_bid=0.06, data_in=0.002)  # $2 per TB in, a cent an hour dearer
    moving_500_gb = offers.Transfer(in_gb=500, out_gb=0, runtime_hours=Decimal(8))

    assert _choose([cheap_hourly, cheap_transfer]).offer.id == "1"
    assert _choose([cheap_hourly, cheap_transfer], moving_500_gb).offer.id == "2"


def test_without_an_estimate_transfer_prices_only_break_ties() -> None:
    same_hourly_dear = _o(1, min_bid=0.05, data_in=0.01)
    same_hourly_cheap = _o(2, min_bid=0.05, data_in=0.002)

    assert _choose([same_hourly_dear, same_hourly_cheap]).offer.id == "2"


# -- The member's estimate when submitting ------------------------------------------------------------


def _spec(**overrides) -> JobSpec:
    return JobSpec(
        name="train",
        image="ghcr.io/alice/train:v3",
        command="python train.py",
        gpu_names=("RTX 3060",),
        target_microusd=70_000,
        max_microusd=100_000,
        **overrides,
    )


def _queue(db_session, member, project, *, confirm=False, **overrides):  # noqa: F811
    return compute_jobs.submit_job(
        db_session,
        member,
        project=project,
        spec=_spec(**overrides),
        cap_cents=500,
        now=NOW,
        weave_client=current_weave[0],
        confirm_transfer_cost=confirm,
    )


def test_a_job_without_an_estimate_is_queued_without_asking(db_session, member, project) -> None:  # noqa: F811
    job = _queue(db_session, member, project)

    assert job.state is ComputeJobState.QUEUED
    assert (job.data_in_gb, job.data_out_gb) == (None, None)


def test_a_small_estimate_is_queued_without_asking(db_session, member, project) -> None:  # noqa: F811
    job = _queue(db_session, member, project, data_in_gb=100)  # at most $2 of a $100 budget

    assert job.data_in_gb == 100 and job.data_out_gb is None


def test_an_estimate_that_could_cost_the_warning_share_needs_confirming(db_session, member, project) -> None:  # noqa: F811
    with pytest.raises(ConfirmationRequired) as raised:
        _queue(db_session, member, project, data_in_gb=400, data_out_gb=100)  # 500 GB at $20/TB = $10, 10%

    message = str(raised.value)
    assert "could cost up to $10.00" in message
    assert "400 GB into the machine and 100 GB out" in message
    assert "$20.00 per TB each way" in message
    assert "10% of the project's $100.00 budget" in message
    assert "pulls the image and restores checkpoints again" in message

    job = _queue(db_session, member, project, confirm=True, data_in_gb=400, data_out_gb=100)
    assert job.state is ComputeJobState.QUEUED
    assert (job.data_in_gb, job.data_out_gb) == (400, 100)


def test_an_estimate_over_whats_left_says_so(db_session, member, admin, project) -> None:  # noqa: F811
    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"transfer_warn_percent": 100}, reason="Only past the budget."
    )

    with pytest.raises(ConfirmationRequired, match=r"more than the \$100.00 left in the project's budget"):
        _queue(db_session, member, project, data_out_gb=6_000)  # $120


def test_the_warning_follows_the_projects_cap(db_session, member, admin, project) -> None:  # noqa: F811
    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"transfer_cap_microusd_per_gb": 5_000}, reason="Cheap hosts."
    )

    job = _queue(db_session, member, project, data_in_gb=1_000)  # at most $5: under 10%

    assert job.data_in_gb == 1_000


@pytest.mark.parametrize("field", ["data_in_gb", "data_out_gb"])
@pytest.mark.parametrize("value", [-1, compute_jobs.MAX_DATA_GB + 1])
def test_an_estimate_must_be_0_to_100_tb(db_session, member, project, field, value) -> None:  # noqa: F811
    with pytest.raises(ValidationFailed) as raised:
        _queue(db_session, member, project, **{field: value})

    assert field in raised.value.errors


# -- Vast's transfer charges --------------------------------------------------------------------------


def _warnings(db_session, job) -> list[ComputeEvent]:
    return list(
        db_session.scalars(
            sa.select(ComputeEvent).where(ComputeEvent.job_id == job.id, ComputeEvent.kind == "transfer_warning")
        )
    )


def _audits(db_session) -> list[AuditEvent]:
    return list(db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == AUDIT_TRANSFER_WARNING)))


def test_transfer_charges_are_kept_per_machine(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    fake.add_charge(row.provider_instance_id, gpu="0.058", disk="0.009", data_in="0.25", data_out="0.5")

    _reconcile(db_session, client)

    assert row.transfer_charged_microusd == 750_000
    assert row.charged_microusd == 817_000  # the whole charge, transfer included
    assert _warnings(db_session, job) == []  # $0.75 of $100


def test_transfer_past_the_warning_share_warns_the_team_once(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    fake.add_charge(row.provider_instance_id, gpu="0.058", disk="0.009", data_in="10.5", data_out="0.5")

    _reconcile(db_session, client)
    _reconcile(db_session, client, minutes=125)

    (event,) = _warnings(db_session, job)
    assert event.message.startswith("Data transfer has cost $11.00 so far, 11% of the project's $100.00 budget.")
    (audit,) = _audits(db_session)
    assert audit.project_id == project.id and audit.actor_id is None
    assert audit.payload["owner_id"] == str(member.user.id)
    assert audit.payload["job_id"] == str(job.id) and audit.payload["job_name"] == "train"
    assert (audit.payload["transfer_cents"], audit.payload["percent"], audit.payload["level"]) == (1_100, 11, 1)


def test_the_pass_that_finds_the_transfer_warns(db_session, member, admin, project, client, fake, weave) -> None:  # noqa: F811
    # The 2026-11-06 paid round: a small transfer on a small budget. Krater's sessions don't autoflush (krater.db), and
    # with a charge this close to the estimate no mismatch audit flushes either, so the warning came a pass late.
    project_service.admin_adjust_budget(
        db_session, admin, project=project, amount_cents=-9_900, reason="A $1 test budget.", weave_client=weave
    )
    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"transfer_warn_percent": 1}, reason="Warn early."
    )
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    fake.add_charge(row.provider_instance_id, gpu="0.055", disk="0.009", data_in="0.003")

    with db_session.no_autoflush:
        result = _reconcile(db_session, client)

    assert result.alerts == 0  # nothing else flushes on the way
    assert row.transfer_charged_microusd == 3_000
    (event,) = _warnings(db_session, job)
    assert event.message.startswith("Data transfer has cost $0.01 so far, 1% of the project's $1.00 budget.")


def test_each_further_share_warns_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    fake.add_charge(row.provider_instance_id, gpu="0.058", disk="0.009", data_in="11")
    _reconcile(db_session, client)
    fake.add_charge(row.provider_instance_id, gpu="0.058", disk="0.009", data_in="10", start=1, end=2)

    _reconcile(db_session, client, minutes=125)

    assert [e.payload["level"] for e in _warnings(db_session, job)] == [1, 2]
    assert [a.payload["percent"] for a in sorted(_audits(db_session), key=lambda a: a.payload["level"])] == [11, 21]


def test_the_warning_share_follows_the_projects_override(db_session, member, admin, project, client, fake) -> None:  # noqa: F811
    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"transfer_warn_percent": 50}, reason="Expected to move a lot."
    )
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    fake.add_charge(row.provider_instance_id, gpu="0.058", disk="0.009", data_in="20")

    _reconcile(db_session, client)

    assert row.transfer_charged_microusd == 20_000_000
    assert _warnings(db_session, job) == []  # 20% is under the project's 50%
