"""The Krater Compute scheduler, end to end against FakeVast: start when cheap, budget holds, lost creates, the sweep,
pauses, moves, runtime limits and cancelling."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from krater.compute import scheduler
from krater.compute.search_pacing import SearchPacer
from krater.config import Settings, get_settings
from krater.models import (
    AuditEvent,
    BudgetEntryKind,
    ComputeEscalation,
    ComputeEvent,
    ComputeInstance,
    ComputeInstanceState,
    ComputeJob,
    ComputeJobState,
    ComputeOutbid,
    ProjectStatus,
)
from krater.providers.errors import ProviderUnavailableError
from krater.services import budget, compute_jobs, compute_watch
from krater.services.compute_jobs import JobSpec
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.vast import FakeVast, VastClient
from tests.compute.conftest import current_weave
from tests.factories import new_project

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
SETTINGS = Settings(compute_mode="vast", compute_max_hourly_cost_cents=500)
# Disk is 32 GB everywhere. RTX 3060 offers: 1001 bids $0.055 + $0.0088 disk = $0.0638/h (cheapest),
# 1003 $0.066 + $0.0066 = $0.0726/h, 1002 $0.0495 + $0.0351 = $0.0846/h.
CHEAPEST_3060_FULL = 63_767


def _submit(db_session, member, project, *, now=NOW, **overrides) -> ComputeJob:  # noqa: F811
    spec = {
        "name": "train",
        "image": "ghcr.io/alice/train:v3",
        "command": "python train.py",
        "gpu_names": ("RTX 3060",),
        "target_microusd": 70_000,
        "max_microusd": 100_000,
        **overrides,
    }
    return compute_jobs.submit_job(
        db_session,
        member,
        project=project,
        spec=JobSpec(**spec),
        cap_cents=500,
        now=now,
        weave_client=current_weave[0],
    )


RUNNER_SHA = "ab" * 32


def _pass(db_session, client, now=NOW) -> scheduler.PassResult:
    return scheduler.run_pass(
        db_session, client, SETTINGS, now=now, runner_sha256=RUNNER_SHA, weave_client=current_weave[0]
    )


def _passes(db_session, client, first: int, last: int) -> None:
    """A pass every minute from `first` to `last` (inclusive), as the worker runs them."""
    for minute in range(first, last + 1):
        _pass(db_session, client, NOW + timedelta(minutes=minute))


def _token(fake, row) -> str:
    """The runner token Krater sent to the machine (it keeps only the hash)."""
    env = fake.instances[row.provider_instance_id]["env"]
    return env.split("KRATER_JOB_TOKEN=")[1].split("'")[0].split(" ")[0]


def _runner_starts(db_session, fake, row, now):
    from krater.compute import runner_api

    runner_api.start(
        db_session, runner_api.authenticate(db_session, _token(fake, row)), now=now, settings=get_settings()
    )


def _beat(db_session, row, now):
    from krater.compute import runner_api

    runner_api.heartbeat(db_session, row, {"gpu_util": 90}, now=now)


def _instance(db_session, job) -> ComputeInstance:
    db_session.refresh(job)
    return job.instances[-1]


def _events(db_session, job) -> list[str]:
    return list(
        db_session.scalars(sa.select(ComputeEvent.kind).where(ComputeEvent.job_id == job.id).order_by(ComputeEvent.at))
    )


# -- Start when cheap ---------------------------------------------------------------------------------


def test_a_job_waits_while_every_offer_is_over_its_target(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, target_microusd=50_000)

    result = _pass(db_session, client)

    assert result.rented == []
    assert job.state is ComputeJobState.QUEUED
    assert "at or under $0.0500/h" in job.state_reason
    assert "cheapest now $0.0638/h" in job.state_reason
    assert fake.instances == {}


def test_a_job_rents_the_cheapest_offer_at_or_under_its_target(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    result = _pass(db_session, client)

    row = _instance(db_session, job)
    assert result.rented == [row.label]
    assert row.label == f"krater-job-{job.id.hex[:12]}-1"
    assert row.state is ComputeInstanceState.PROVISIONING
    assert row.provider_offer_id == "1001"
    assert row.interruptible is True
    assert row.gpu_microusd == 55_000  # min_bid $0.05 plus the 10% margin
    assert row.full_microusd == CHEAPEST_3060_FULL
    created = fake.instances[row.provider_instance_id]
    assert created["is_bid"] is True
    assert created["label"] == row.label
    # The image's ENTRYPOINT is replaced by sh (in args mode, Vast's onstart is the entrypoint override).
    assert created["onstart"] == "sh"
    assert created["args"][0] == "-c"
    # The bootstrap fetches the runner and checks its sha256; the runner asks Krater for the command.
    assert RUNNER_SHA in created["args"][1]
    assert "python train.py" not in created["args"][1]
    assert created["runtype"] == "args"
    assert "KRATER_JOB_ID=" in created["env"]
    assert "KRATER_RUNNER_URL=http://localhost:8000" in created["env"]
    token = _token(fake, row)
    assert len(token) >= 40 and row.runner_token_hash != token
    assert job.state is ComputeJobState.STARTING
    # Holds its max runtime (8 h) at the rented price: ceil(0.063767 * 8 * 100) cents.
    assert job.hold_cents == 52
    # The search asked for this job's shape at the reliability floor.
    body = fake.search_bodies[-1]
    assert body["allocated_storage"] == 32 and body["reliability"] == {"gte": 0.95} and body["type"] == "bid"


def test_the_job_runs_once_vast_reports_running(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)

    _pass(db_session, client, NOW + timedelta(minutes=1))  # still pulling
    assert job.state is ComputeJobState.STARTING
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert row.state is ComputeInstanceState.RUNNING
    assert job.state is ComputeJobState.RUNNING
    assert job.started_at == NOW + timedelta(minutes=5)
    assert _events(db_session, job) == ["queued", "renting", "rented", "running"]


def test_an_on_demand_job_pays_dph_base(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, interruptible=False, target_microusd=80_000)

    _pass(db_session, client)

    row = _instance(db_session, job)
    assert row.interruptible is False
    assert row.gpu_microusd == 60_000
    assert fake.instances[row.provider_instance_id]["is_bid"] is False


def test_two_jobs_never_rent_the_same_offer(db_session, member, project, client) -> None:  # noqa: F811
    first = _submit(db_session, member, project, target_microusd=90_000)
    second = _submit(db_session, member, project, target_microusd=90_000, now=NOW + timedelta(seconds=1))

    _pass(db_session, client, NOW + timedelta(seconds=2))

    assert {_instance(db_session, first).provider_offer_id, _instance(db_session, second).provider_offer_id} == {
        "1001",
        "1003",
    }


# -- Budget and caps -----------------------------------------------------------------------------------


def test_no_rental_without_budget_for_one_hour(db_session, member, project, client, fake, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr("krater.compute.scheduler.budget.remaining_cents", lambda session, project: 6)
    job = _submit(db_session, member, project)

    _pass(db_session, client)

    assert job.state is ComputeJobState.QUEUED
    assert job.state_reason.startswith("Waiting for budget")
    assert fake.instances == {}


def test_holds_keep_two_jobs_from_spending_the_same_dollars(db_session, member, project, client, monkeypatch) -> None:  # noqa: F811
    # 60 cents left: the first job holds 52 (8 h at $0.0638/h); the second needs 8 for one hour and has 8.
    monkeypatch.setattr("krater.compute.scheduler.budget.remaining_cents", lambda session, project: 60)
    first = _submit(db_session, member, project, target_microusd=90_000)
    second = _submit(db_session, member, project, target_microusd=90_000, now=NOW + timedelta(seconds=1))
    third = _submit(db_session, member, project, target_microusd=90_000, now=NOW + timedelta(seconds=2))

    _pass(db_session, client, NOW + timedelta(seconds=3))

    assert first.state is ComputeJobState.STARTING and first.hold_cents == 52
    assert second.state is ComputeJobState.STARTING and second.hold_cents == 8  # what was left
    assert third.state is ComputeJobState.QUEUED and third.state_reason.startswith("Waiting for budget")


def test_a_lowered_hourly_cap_wins_over_the_target(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    project.max_hourly_cost_cents = 5  # $0.05/h: under every offer, even at its minimum bid
    db_session.flush()

    _pass(db_session, client)

    assert job.state is ComputeJobState.QUEUED
    assert "at or under $0.0500/h" in job.state_reason
    assert fake.instances == {}


def test_a_finished_project_cancels_its_queued_jobs(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    project.status = ProjectStatus.WITHDRAWN
    db_session.flush()

    _pass(db_session, client)

    assert job.state is ComputeJobState.CANCELLED


# -- Max wait --------------------------------------------------------------------------------------------


def test_after_max_wait_a_job_can_switch_to_on_demand_up_to_its_max(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session,
        member,
        project,
        target_microusd=40_000,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.ON_DEMAND,
    )
    _pass(db_session, client)
    assert job.state is ComputeJobState.QUEUED

    _pass(db_session, client, NOW + timedelta(minutes=61))

    row = _instance(db_session, job)
    assert row.interruptible is False
    assert row.full_microusd <= 100_000


def test_after_max_wait_a_job_can_raise_its_bid_to_its_max(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session,
        member,
        project,
        target_microusd=40_000,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.RAISE_BID,
    )

    _pass(db_session, client, NOW + timedelta(minutes=61))

    assert _instance(db_session, job).interruptible is True


def test_after_max_wait_a_job_can_cancel(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session, member, project, target_microusd=40_000, max_wait_minutes=60, on_max_wait=ComputeEscalation.CANCEL
    )

    _pass(db_session, client, NOW + timedelta(minutes=61))

    assert job.state is ComputeJobState.CANCELLED


# -- Lost creates and the sweep ----------------------------------------------------------------------------


# -- Vast's daily search quota (staging, 2026-10-11) ----------------------------------------------------


def _paced_pass(db_session, client, pacer, now) -> scheduler.PassResult:
    return scheduler.run_pass(
        db_session, client, SETTINGS, now=now, runner_sha256=RUNNER_SHA, weave_client=current_weave[0], pacer=pacer
    )


def test_searches_ask_for_a_limited_number_of_rows(db_session, member, project, client, fake) -> None:  # noqa: F811
    _submit(db_session, member, project, target_microusd=50_000)

    _pass(db_session, client)

    assert fake.search_bodies[0]["limit"] == scheduler.SEARCH_LIMIT


def test_a_search_asks_only_for_offers_under_the_jobs_price(db_session, member, project, client, fake) -> None:  # noqa: F811
    # Vast's quota counts rows, so a job waiting for a low price should cost none.
    job = _submit(db_session, member, project, target_microusd=20_000)

    _pass(db_session, client)

    assert fake.search_bodies[0]["min_bid"] == {"lte": 0.02}
    assert job.state is ComputeJobState.QUEUED
    assert job.state_reason == "Waiting for a bid offer at or under $0.0200/h (none right now; see the price history)."


def test_an_on_demand_search_filters_on_the_full_price(db_session, member, project, client, fake) -> None:  # noqa: F811
    _submit(db_session, member, project, interruptible=False, target_microusd=90_000, max_microusd=100_000)

    _pass(db_session, client)

    assert fake.search_bodies[0]["dph_total"] == {"lte": 0.09}
    assert "min_bid" not in fake.search_bodies[0]


def test_a_waiting_job_is_searched_for_less_and_less_often(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, target_microusd=50_000)
    pacer = SearchPacer()

    searched = []
    for minute in range(10):
        before = len(fake.search_bodies)
        _paced_pass(db_session, client, pacer, NOW + timedelta(minutes=minute, seconds=2))
        if len(fake.search_bodies) > before:
            searched.append(minute)

    assert searched == [0, 1, 2, 4, 8]
    assert job.state is ComputeJobState.QUEUED
    assert "cheapest now $0.0638/h" in job.state_reason  # the reason from its last search stays


def test_an_escalated_job_searches_at_once(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(
        db_session, member, project, target_microusd=40_000, max_wait_minutes=3, on_max_wait=ComputeEscalation.RAISE_BID
    )
    pacer = SearchPacer()
    for minute in range(3):  # searched at 0, 1 and 2; the next would be at 4
        _paced_pass(db_session, client, pacer, NOW + timedelta(minutes=minute, seconds=2))
    assert job.state is ComputeJobState.QUEUED

    _paced_pass(db_session, client, pacer, NOW + timedelta(minutes=3, seconds=2))

    assert job.state is ComputeJobState.STARTING


def test_with_the_search_quota_used_up_jobs_wait_for_the_reset(db_session, member, project, client, fake) -> None:  # noqa: F811
    fake.set_search_quota(20_000, remaining=0, reset_at=int(datetime(2026, 10, 12, tzinfo=UTC).timestamp()))
    job = _submit(db_session, member, project)
    pacer = SearchPacer()

    result = _paced_pass(db_session, client, pacer, NOW)

    assert result.errors == 0
    assert job.state is ComputeJobState.QUEUED
    assert "Vast.ai's daily search quota is used up until 00:00 UTC" in job.state_reason  # the provider's title
    _paced_pass(db_session, client, pacer, NOW + timedelta(minutes=5))
    assert len(fake.search_bodies) == 1  # nothing more is asked of Vast until the reset

    fake.set_search_quota(20_000, reset_at=int(datetime(2026, 10, 13, tzinfo=UTC).timestamp()))
    _paced_pass(db_session, client, pacer, datetime(2026, 10, 12, 0, 1, tzinfo=UTC))
    assert job.state is ComputeJobState.STARTING


def test_each_pass_saves_the_quota_for_admins(db_session, member, project, client, fake) -> None:  # noqa: F811
    from krater.compute import search_quota

    fake.set_search_quota(20_000, remaining=12_000)
    _submit(db_session, member, project, target_microusd=50_000)

    _pass(db_session, client)

    row = search_quota.latest(db_session)
    assert row.limit_rows == 20_000
    assert row.remaining_rows < 12_000


def _timing_out_client(fake: FakeVast) -> VastClient:
    """A client whose create reaches Vast but whose response is lost."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if request.method == "PUT" and "/asks/" in request.url.path:
            raise httpx.ReadTimeout("response lost")
        return response

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_a_create_whose_response_was_lost_is_found_by_its_label(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    _pass(db_session, _timing_out_client(fake))
    row = _instance(db_session, job)
    assert row.state is ComputeInstanceState.CREATING and row.provider_instance_id is None
    assert len(fake.instances) == 1  # Vast did rent it

    _pass(db_session, client, NOW + timedelta(minutes=1))

    assert row.state is ComputeInstanceState.PROVISIONING
    assert row.provider_instance_id == str(next(iter(fake.instances)))
    assert len(fake.instances) == 1  # not swept, not rented twice


def test_a_create_that_never_appears_is_given_up_and_requeued(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, _timing_out_client(fake))
    row = _instance(db_session, job)
    fake.instances.clear()  # it never really happened

    _pass(db_session, client, NOW + timedelta(minutes=1))
    assert row.state is ComputeInstanceState.CREATING
    fake.offers[1001]["rented"] = False
    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert row.state is ComputeInstanceState.CREATE_FAILED
    # Requeued and, the same pass, rented again as attempt 2.
    assert _instance(db_session, job).attempt == 2
    assert job.state is ComputeJobState.STARTING


def test_the_sweep_destroys_krater_instances_krater_does_not_know(db_session, client, fake) -> None:
    fake.add_offer(2001, 2001, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    fake.add_offer(2002, 2002, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    from krater.vast import CreateInstance

    stray = client.create_instance(2001, CreateInstance(image="x", disk_gb=8, label="krater-job-deadbeef0000-1"))
    other = client.create_instance(2002, CreateInstance(image="x", disk_gb=8, label="krater-spike-123-A"))

    result = _pass(db_session, client)

    assert result.swept == [stray]
    assert stray not in fake.instances
    assert other in fake.instances  # not Krater's label prefix: never touched


def test_a_refused_create_puts_the_job_back_in_its_place(db_session, member, project, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/asks/" in request.url.path:
            # Someone else rented it between the search and the create.
            return httpx.Response(400, json={"success": False, "error": "invalid_args", "msg": "GPU conflict"})
        return fake.handle(request)

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    _pass(db_session, client)

    assert _instance(db_session, job).state is ComputeInstanceState.CREATE_FAILED
    assert job.state is ComputeJobState.QUEUED
    assert job.queued_at == NOW
    assert job.hold_cents == 0
    assert fake.instances == {}


def test_a_create_answered_200_without_success_is_settled_by_its_label(db_session, member, project, fake) -> None:  # noqa: F811
    """Seen on staging: Vast answered `{"success": false}` with no message and rented the machine anyway."""
    job = _submit(db_session, member, project)

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if request.method == "PUT" and "/asks/" in request.url.path:
            return httpx.Response(200, json={"success": False})
        return response

    flaky = fake.client()
    flaky._http = httpx.Client(transport=httpx.MockTransport(handler))
    _pass(db_session, flaky)
    row = _instance(db_session, job)
    assert row.state is ComputeInstanceState.CREATING and job.state is ComputeJobState.STARTING
    assert len(fake.instances) == 1

    _pass(db_session, fake.client(), NOW + timedelta(minutes=1))

    assert row.state is ComputeInstanceState.PROVISIONING
    assert row.provider_instance_id == str(next(iter(fake.instances)))
    assert len(fake.instances) == 1  # adopted: not swept, not rented again


def test_a_redirected_create_is_still_a_refusal(db_session, member, project, fake) -> None:  # noqa: F811
    """A 308 means a path without its trailing slash: nothing happened, and it's a bug, not an unknown outcome."""
    job = _submit(db_session, member, project)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/asks/" in request.url.path:
            return httpx.Response(308, headers={"location": str(request.url)})
        return fake.handle(request)

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    _pass(db_session, client)

    assert _instance(db_session, job).state is ComputeInstanceState.CREATE_FAILED
    assert job.state is ComputeJobState.QUEUED


# -- Pauses, moves, limits ----------------------------------------------------------------------------------


def _running_job(db_session, member, project, client, fake, **overrides):  # noqa: F811
    job = _submit(db_session, member, project, **overrides)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=5))
    return job, row


def test_an_outbid_job_waits_raises_its_bid_then_moves(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    vast_id = row.provider_instance_id
    fake.set_status(vast_id, "stopped")

    _pass(db_session, client, NOW + timedelta(minutes=6))
    assert job.state is ComputeJobState.PAUSED and row.state is ComputeInstanceState.PAUSED

    _pass(db_session, client, NOW + timedelta(minutes=8))  # inside the 5-minute grace
    assert row.gpu_microusd == 55_000

    _pass(db_session, client, NOW + timedelta(minutes=12))  # grace over: bid +10%
    assert row.gpu_microusd == 60_500
    assert fake.instances[vast_id]["dph_base"] == pytest.approx(0.0605)

    _pass(db_session, client, NOW + timedelta(minutes=37))  # paused 31 minutes: move
    assert row.state is ComputeInstanceState.DESTROYED
    assert vast_id not in fake.instances
    assert "bid_raised" in _events(db_session, job)
    # Requeued, and in the same pass rented again elsewhere as attempt 2, which resumes.
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.provider_instance_id != vast_id
    assert "KRATER_RESUMED=1" in fake.instances[new.provider_instance_id]["env"]


def test_a_move_whose_gone_check_was_rate_limited_still_moves(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Staging, 2026-10-11: the destroy went through, the check that it was gone got a 429, and the next pass found
    the machine gone but left the job paused with nothing running."""
    job, row = _running_job(db_session, member, project, client, fake)
    vast_id = row.provider_instance_id
    fake.set_status(vast_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
        elif deleted and request.method == "GET" and f"/instances/{vast_id}/" in request.url.path:
            return httpx.Response(429, json={"detail": "slow down"})
        return fake.handle(request)

    limited = fake.client()
    limited._http = httpx.Client(transport=httpx.MockTransport(handler))
    _pass(db_session, limited, NOW + timedelta(minutes=37))
    assert vast_id not in fake.instances and row.state is not ComputeInstanceState.DESTROYED

    _pass(db_session, client, NOW + timedelta(minutes=38))

    assert row.state is ComputeInstanceState.DESTROYED
    new = _instance(db_session, job)
    assert new.attempt == 2 and job.state is ComputeJobState.STARTING


def test_an_active_job_left_without_a_machine_is_requeued(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    row.state = ComputeInstanceState.DESTROYED  # however it happened
    fake.instances.pop(row.provider_instance_id)
    db_session.commit()

    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert "requeued" in _events(db_session, job)
    assert _instance(db_session, job).attempt == 2


def test_a_paused_job_that_resumes_runs_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.provider_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))

    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.RUNNING and row.paused_since is None


def _limit_order(db_session, member, project, client, fake, limit=65_000):  # noqa: F811
    """A limit order: one price, and it holds its machine when outbid. Rented on 1001 ($0.0638/h); the other 3060s
    can't be had under $0.0666/h (1003's min bid plus disk) and $0.0801/h, over the limit."""
    job, row = _running_job(
        db_session,
        member,
        project,
        client,
        fake,
        target_microusd=limit,
        max_microusd=limit,
        on_outbid=ComputeOutbid.HOLD,
    )
    _beat(db_session, row, NOW + timedelta(minutes=6))
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))
    return job, row


def test_a_limit_order_holds_its_outbid_machine_while_nothing_else_is_under_its_limit(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _limit_order(db_session, member, project, client, fake)
    assert job.state is ComputeJobState.PAUSED and "keeps the machine" in job.state_reason

    _passes(db_session, client, 7, 90)  # well past the 30-minute "move after"

    assert row.state is ComputeInstanceState.PAUSED and row.provider_instance_id in fake.instances
    assert job.attempts == 1 and "Keeping this machine" in job.state_reason
    assert row.full_microusd <= 65_000  # the bid rose to the limit, and no further
    assert 1 <= _events(db_session, job).count("bid_raised") <= 3  # not once a minute at the limit

    fake.set_status(row.provider_instance_id, "running", intended_status="running")  # the market came back down
    _pass(db_session, client, NOW + timedelta(minutes=91))
    assert job.state is ComputeJobState.RUNNING and row.state is ComputeInstanceState.RUNNING


def test_a_limit_order_moves_once_another_machine_is_free_under_its_limit(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    job, row = _limit_order(db_session, member, project, client, fake)
    vast_id = row.provider_instance_id
    _passes(db_session, client, 7, 40)
    assert job.attempts == 1

    fake.add_offer(2001, 2001, "RTX 3060", dph_base=0.08, min_bid=0.040, storage_cost=0.10, gpu_ram_mb=12288)
    _pass(db_session, client, NOW + timedelta(minutes=41))

    assert vast_id not in fake.instances and "moving" in _events(db_session, job)
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.provider_offer_id == "2001"
    assert "KRATER_RESUMED=1" in fake.instances[new.provider_instance_id]["env"]


def test_an_ordinary_job_still_moves_after_the_move_time(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")
    _passes(db_session, client, 6, 37)

    assert row.state is ComputeInstanceState.DESTROYED and job.on_outbid is ComputeOutbid.MOVE


def test_a_limit_order_needs_an_interruptible_machine(db_session, member, project) -> None:  # noqa: F811
    with pytest.raises(ValidationFailed) as caught:
        _submit(db_session, member, project, interruptible=False, on_outbid=ComputeOutbid.HOLD)

    assert "on_outbid" in caught.value.errors


def test_the_bid_never_goes_over_the_jobs_max(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, max_microusd=70_000)
    fake.set_status(row.provider_instance_id, "stopped")
    for minute in range(6, 29):
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert row.full_microusd <= 70_000
    # At the max it stays put: no change-bid call, and no event, every minute (it did both).
    assert _events(db_session, job).count("bid_raised") <= 3


def test_max_runtime_destroys_the_machine_and_fails_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, max_runtime_minutes=60)

    _pass(db_session, client, NOW + timedelta(minutes=66))

    assert job.state is ComputeJobState.FAILED
    assert "max runtime" in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}
    assert job.hold_cents == 0


def test_a_machine_that_never_starts_is_given_up(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)

    _pass(db_session, client, NOW + timedelta(minutes=31))

    assert job.state is ComputeJobState.FAILED
    assert fake.instances == {}


CDI_ERROR = "Error response from daemon: failed to create task for container: failed to inject CDI devices"


def _host_fails(fake, row) -> None:
    """What Vast showed for a host that couldn't give the container its GPU (staging, 2026-10-15)."""
    fake.set_status(row.provider_instance_id, "created", intended_status="stopped", status_msg=CDI_ERROR)


def test_a_host_that_cannot_start_the_container_is_left_for_another_machine(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    assert row.provider_machine_id == "501"
    _host_fails(fake, row)

    _pass(db_session, client, NOW + timedelta(minutes=4))  # inside the grace: Vast may still start it
    assert row.state is ComputeInstanceState.PROVISIONING

    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert row.state is ComputeInstanceState.DESTROYED and row.destroy_reason == scheduler.HOST_FAILED
    assert row.provider_instance_id not in fake.instances
    event = db_session.scalars(
        sa.select(ComputeEvent).where(ComputeEvent.job_id == job.id, ComputeEvent.kind == "host_failed")
    ).one()
    assert event.payload["provider_message"] == CDI_ERROR
    # Requeued and, in the same pass, rented on another machine though 501 is still the cheapest.
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.provider_machine_id != "501"
    assert job.state is ComputeJobState.STARTING


DENIED = 'Error response from daemon: Head "https://ghcr.io/v2/you/train/manifests/v1.": denied'


@pytest.mark.parametrize(
    "status",
    [
        {"actual_status": "loading"},  # what Vast showed within a minute
        {"actual_status": "created", "intended_status": "stopped"},  # and a few minutes later
    ],
)
def test_an_image_the_registry_refuses_fails_the_job_at_once(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
    status,
) -> None:
    """Production, 2026-11-12: a box's image didn't exist, and Krater rented three machines, five minutes each,
    before failing it."""
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    status = dict(status)
    fake.set_status(row.provider_instance_id, status.pop("actual_status"), status_msg=DENIED, **status)

    _pass(db_session, client, NOW + timedelta(minutes=1))

    assert job.state is ComputeJobState.FAILED
    assert "couldn't be pulled" in job.state_reason and 'manifests/v1.": denied' in job.state_reason
    assert job.image in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED and row.destroy_reason == scheduler.IMAGE_PULL_FAILED
    assert fake.instances == {}
    assert len(job.instances) == 1
    event = db_session.scalars(
        sa.select(ComputeEvent).where(ComputeEvent.job_id == job.id, ComputeEvent.kind == "image_pull_failed")
    ).one()
    assert event.payload["provider_message"] == DENIED


@pytest.mark.parametrize(
    "message",
    [
        "Error response from daemon: manifest for ghcr.io/you/train:v2 not found: manifest unknown: manifest unknown",
        "Error response from daemon: pull access denied for nosuch/image, repository does not exist or may require "
        "'docker login': denied: requested access to the resource is denied",
        'Error response from daemon: Head "https://ghcr.io/v2/org/private/manifests/latest": unauthorized',
        "Error response from daemon: invalid reference format: repository name (library/Train) must be lowercase",
    ],
)
def test_registry_refusals_are_recognised(message) -> None:
    assert scheduler.pull_refused(message)


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "pulling image",
        CDI_ERROR,
        'Error response from daemon: Head "https://registry-1.docker.io/v2/library/python/manifests/3.12": '
        "toomanyrequests: You have reached your pull rate limit.",
        'Error response from daemon: Get "https://ghcr.io/v2/": net/http: request canceled while waiting for '
        "connection (Client.Timeout exceeded while awaiting headers)",
        'Error response from daemon: Get "https://ghcr.io/v2/": dial tcp: lookup ghcr.io: no such host',
        'Error response from daemon: failed to create task for container: exec: "nvidia-container-runtime-hook": '
        "executable file not found in $PATH",
        "denied",  # not from Docker
    ],
)
def test_passing_and_host_errors_are_not_refusals(message) -> None:
    assert not scheduler.pull_refused(message)


def test_a_refusal_after_the_image_has_run_waits_for_the_host(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    """The image pulled before, so the registry's refusal now is more likely a passing fault than a wrong name."""
    job, row = _running_job(db_session, member, project, client, fake)
    row.state = ComputeInstanceState.DESTROYED
    fake.instances.pop(row.provider_instance_id)
    db_session.commit()
    _pass(db_session, client, NOW + timedelta(minutes=7))
    second = _instance(db_session, job)
    assert second.attempt == 2
    fake.set_status(second.provider_instance_id, "loading", status_msg=DENIED)

    _pass(db_session, client, NOW + timedelta(minutes=8))

    assert job.state is not ComputeJobState.FAILED
    assert second.state is ComputeInstanceState.PROVISIONING


def test_a_passing_pull_error_still_waits_for_the_host(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(
        row.provider_instance_id,
        "loading",
        status_msg='Error response from daemon: Head "https://registry-1.docker.io/v2/x/y/manifests/1": '
        "toomanyrequests",
    )

    _pass(db_session, client, NOW + timedelta(minutes=1))

    assert job.state is ComputeJobState.STARTING
    assert row.state is ComputeInstanceState.PROVISIONING


def test_a_later_attempt_doesnt_claim_to_resume_when_nothing_ran_before(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    _host_fails(fake, _instance(db_session, job))
    _pass(db_session, client, NOW + timedelta(minutes=5))
    second = _instance(db_session, job)
    assert second.attempt == 2

    _runner_starts(db_session, fake, second, NOW + timedelta(minutes=6))

    said = db_session.scalars(
        sa.select(ComputeEvent.message).where(ComputeEvent.job_id == job.id, ComputeEvent.kind == "runner_started")
    ).one()
    assert said == "The runner started (attempt 2; no earlier attempt got this far)."


def test_a_job_whose_containers_never_start_anywhere_fails(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, target_microusd=100_000)  # all three RTX 3060 machines qualify
    _pass(db_session, client)
    minute = 0
    for _ in range(scheduler.MAX_HOST_FAILURES):
        _host_fails(fake, _instance(db_session, job))
        minute += 5
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert job.state is ComputeJobState.FAILED
    assert "couldn't start the container" in job.state_reason
    assert fake.instances == {}
    assert len({row.provider_machine_id for row in job.instances}) == scheduler.MAX_HOST_FAILURES


def test_a_machine_vast_refused_and_left_stopped_is_dropped_at_once(db_session, member, project, fake) -> None:  # noqa: F811
    """Staging, 2026-11-12: Vast answered a bid's create with `success: false` and an instance id, and left the instance
    stopped. Krater waited out HOST_START_GRACE on each one, and a job failed after three, blaming its image."""
    job = _submit(db_session, member, project, target_microusd=100_000)

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if request.method == "PUT" and "/asks/" in request.url.path:
            return httpx.Response(200, json={"success": False, "new_contract": response.json()["new_contract"]})
        return response

    refusing = fake.client()
    refusing._http = httpx.Client(transport=httpx.MockTransport(handler))
    _pass(db_session, refusing)
    row = _instance(db_session, job)
    fake.set_status(next(iter(fake.instances)), "loading", intended_status="stopped")

    _pass(db_session, fake.client(), NOW + timedelta(minutes=1))

    assert row.state is ComputeInstanceState.DESTROYED and row.destroy_reason == scheduler.NOT_STARTED
    assert row.provider_instance_id not in fake.instances
    assert "not_started" in _events(db_session, job) and "host_failed" not in _events(db_session, job)
    # Rented elsewhere in the same pass, and its place in the queue kept.
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.provider_machine_id != row.provider_machine_id
    assert job.state is ComputeJobState.STARTING
    assert job.queued_at == NOW


def _unavailable_on(fake, machine: str, *, status: int = 410, error: str = "no_such_ask", msg: str = "unavailable"):
    """A client whose creates on `machine` get Vast's answer to a bid sent with `cancel_unavail` that can't start."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/asks/" in request.url.path:
            bodies.append(json.loads(request.content))
            offer = request.url.path.rstrip("/").rsplit("/", 1)[-1]
            if any(str(key) == offer and str(o["machine_id"]) == machine for key, o in fake.offers.items()):
                return httpx.Response(status, json={"success": False, "error": error, "msg": msg})
        return fake.handle(request)

    refusing = fake.client()
    refusing._http = httpx.Client(transport=httpx.MockTransport(handler))
    return refusing, bodies


def test_a_bid_its_machine_cant_start_is_refused_and_that_machine_avoided(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    """With `cancel_unavail` Vast makes nothing and answers 410 `no_such_ask`. The machine is kept from every job for
    UNAVAILABLE_WINDOW, so the job doesn't keep asking for the same cheapest offer."""
    refusing, bodies = _unavailable_on(fake, "501")
    first = _submit(db_session, member, project, target_microusd=100_000)
    _pass(db_session, refusing)

    row = _instance(db_session, first)
    assert bodies[0]["cancel_unavail"] is True
    assert row.state is ComputeInstanceState.CREATE_FAILED and row.destroy_reason == scheduler.UNAVAILABLE
    assert row.provider_machine_id == "501" and fake.instances == {}
    assert first.state is ComputeJobState.QUEUED and "create_refused" in _events(db_session, first)

    _pass(db_session, refusing, NOW + timedelta(minutes=1))
    assert _instance(db_session, first).provider_machine_id not in ("501", None)

    second = _submit(db_session, member, project, now=NOW + timedelta(minutes=2), target_microusd=100_000)
    _pass(db_session, refusing, NOW + timedelta(minutes=2))
    assert _instance(db_session, second).provider_machine_id not in ("501", None)  # 501 is still the cheapest

    # Past the window it's tried again: the same answer comes when someone else rented the offer first.
    third = _submit(db_session, member, project, now=NOW + timedelta(hours=2), target_microusd=100_000)
    _pass(db_session, fake.client(), NOW + timedelta(hours=2))
    assert _instance(db_session, third).provider_machine_id == "501"


def test_vasts_other_unavailable_answer_avoids_the_machine_too(db_session, member, project, fake) -> None:  # noqa: F811
    """Vast's reference also gives a 404 `invalid_args` whose message names no_such_ask."""
    refusing, _ = _unavailable_on(fake, "501", status=404, error="invalid_args", msg="error 404/3603: no_such_ask")
    job = _submit(db_session, member, project, target_microusd=100_000)
    _pass(db_session, refusing)
    assert _instance(db_session, job).destroy_reason == scheduler.UNAVAILABLE

    _pass(db_session, refusing, NOW + timedelta(minutes=1))
    assert _instance(db_session, job).provider_machine_id not in ("501", None)


def test_a_create_refused_but_started_is_kept(db_session, member, project, fake) -> None:  # noqa: F811
    """The same refusal on 2026-10-11 rented a machine that ran: only a stopped one is dropped."""
    job = _submit(db_session, member, project)

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if request.method == "PUT" and "/asks/" in request.url.path:
            return httpx.Response(200, json={"success": False})
        return response

    refusing = fake.client()
    refusing._http = httpx.Client(transport=httpx.MockTransport(handler))
    _pass(db_session, refusing)
    fake.set_status(next(iter(fake.instances)), "running")

    _pass(db_session, fake.client(), NOW + timedelta(minutes=1))

    row = _instance(db_session, job)
    assert row.attempt == 1 and row.state is ComputeInstanceState.RUNNING


def test_a_machine_that_failed_one_job_is_kept_from_the_others(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    first = _submit(db_session, member, project, target_microusd=100_000)
    _pass(db_session, client)
    assert _instance(db_session, first).provider_machine_id == "501"
    _host_fails(fake, _instance(db_session, first))
    _pass(db_session, client, NOW + timedelta(minutes=5))
    assert _instance(db_session, first).provider_machine_id != "501"

    second = _submit(db_session, member, project, now=NOW + timedelta(minutes=6), target_microusd=100_000)
    _pass(db_session, client, NOW + timedelta(minutes=6))
    assert _instance(db_session, second).provider_machine_id not in ("501", None)  # 501 is still the cheapest

    # Past the window it's tried again: hosts get fixed.
    third = _submit(db_session, member, project, now=NOW + timedelta(hours=7), target_microusd=100_000)
    _pass(db_session, client, NOW + timedelta(hours=7))
    assert _instance(db_session, third).provider_machine_id == "501"


def test_a_machine_whose_runner_reported_is_not_taken_for_a_broken_host(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    # The container starts before Vast says `running`: a runner that reported means the host works.
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=1))
    fake.set_status(row.provider_instance_id, "exited", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert row.state is not ComputeInstanceState.DESTROYED
    assert "host_failed" not in _events(db_session, job)


def test_a_vanished_machine_requeues_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.instances.pop(row.provider_instance_id)
    fake.offers[1001]["rented"] = True  # the host is gone

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert row.state is ComputeInstanceState.DESTROYED
    assert "lost" in _events(db_session, job)
    assert _instance(db_session, job).attempt == 2


def test_a_withdrawn_project_stops_its_running_machines(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    project.status = ProjectStatus.WITHDRAWN
    db_session.flush()

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert job.state is ComputeJobState.CANCELLED
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}
    assert job.hold_cents == 0


def test_a_completed_project_cancels_jobs_as_completed(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Seen on staging: completing a project reclaims its unspent budget, so its running job failed as "reached its
    budget" and Slack got a budget-teardown alert, though nothing overspent."""
    job, row = _running_job(db_session, member, project, client, fake)
    project.status = ProjectStatus.COMPLETED
    budget.add_entry(
        db_session,
        project=project,
        kind=BudgetEntryKind.RECLAIM,
        amount_cents=-budget.remaining_cents(db_session, project),
        actor=member,
    )
    db_session.flush()

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert job.state is ComputeJobState.CANCELLED
    assert job.state_reason == "The project is completed."
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}
    assert not db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == "budget_teardown")).all()


def test_a_settled_create_never_reuses_a_recorded_id(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Seen on staging: a restarted dev worker's fake Vast handed out an id again. Settling the create by its label
    then broke the unique instance id, and every later scheduler pass failed with it."""
    job, first = _running_job(db_session, member, project, client, fake)
    taken = first.provider_instance_id
    first.state = ComputeInstanceState.DESTROYED
    second = ComputeInstance(
        job_id=job.id,
        attempt=2,
        label=first.label[:-1] + "2",
        state=ComputeInstanceState.CREATING,
        provider_offer_id="0",
        provider_machine_id="0",
        gpu_name="RTX 3060",
        interruptible=True,
        gpu_microusd=0,
        disk_microusd=0,
        runner_token_hash="x",
        created_at=NOW,
    )
    db_session.add(second)
    job.attempts, job.state = 2, ComputeJobState.STARTING
    fake.instances[taken]["label"] = second.label  # Vast lists "the same" id under the new label
    db_session.flush()

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert second.provider_instance_id is None
    assert second.state is ComputeInstanceState.CREATE_FAILED


def test_cancelling_destroys_the_machine_on_the_next_pass(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.CANCELLED
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}


def test_a_destroy_vast_does_not_confirm_is_retried(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    real_destroy = fake._destroy
    fake._destroy = lambda instance_id: httpx.Response(200, json={"success": True})  # says yes, does nothing

    _pass(db_session, client, NOW + timedelta(minutes=7))
    assert row.state is ComputeInstanceState.RUNNING and row.destroy_requested_at is not None

    fake._destroy = real_destroy
    _pass(db_session, client, NOW + timedelta(minutes=8))
    assert row.state is ComputeInstanceState.DESTROYED


# -- Submitting ---------------------------------------------------------------------------------------------


def test_only_the_project_team_submits(db_session, member, project, make_actor) -> None:  # noqa: F811
    outsider = make_actor(groups=frozenset({"ganymede:member"}))
    with pytest.raises(NotAllowed):
        _submit(db_session, outsider, project)


def test_prices_are_checked_against_the_cap(db_session, member, project) -> None:  # noqa: F811
    with pytest.raises(ValidationFailed) as excinfo:
        _submit(db_session, member, project, max_microusd=6_000_000)
    assert "max" in excinfo.value.errors
    with pytest.raises(ValidationFailed) as excinfo:
        _submit(db_session, member, project, target_microusd=200_000)
    assert "target" in excinfo.value.errors


def test_a_draft_project_cannot_run_jobs(db_session, member) -> None:  # noqa: F811
    draft = new_project(db_session, member, title="Draft", write_up="w", budget_requested_cents=100)
    with pytest.raises(InvalidState):
        _submit(db_session, member, draft)


def test_bid_rounding_and_clamping() -> None:
    from krater.compute.offers import bid_for
    from krater.vast import Offer

    offer = Offer.from_json(
        {"id": 1, "gpu_name": "x", "num_gpus": 1, "dph_base": 0.1, "dph_total": 0.1, "min_bid": 0.0533333}
    )
    assert bid_for(offer, margin_percent=10, max_gpu=Decimal("1")) == Decimal("0.0587")
    assert bid_for(offer, margin_percent=10, max_gpu=Decimal("0.055")) == Decimal("0.055")
    assert bid_for(offer, margin_percent=10, max_gpu=Decimal("0.05")) is None


# -- The runner ---------------------------------------------------------------------------------------------


def test_a_silent_runner_fails_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    _passes(db_session, client, 6, 19)
    _beat(db_session, row, NOW + timedelta(minutes=20))

    _passes(db_session, client, 20, 49)
    assert job.state is ComputeJobState.RUNNING
    _pass(db_session, client, NOW + timedelta(minutes=50))

    assert job.state is ComputeJobState.FAILED
    assert "stopped reporting" in job.state_reason


def test_krater_being_down_does_not_fail_a_runner_that_reports_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Krater down for over the heartbeat timeout: the runner kept trying, and must not be failed on Krater's first
    pass back, before its next heartbeat gets through."""
    job, row = _running_job(db_session, member, project, client, fake)
    _beat(db_session, row, NOW + timedelta(minutes=6))
    # Krater is down from minute 7 to minute 90.

    _pass(db_session, client, NOW + timedelta(minutes=90))
    assert job.state is ComputeJobState.RUNNING and row.watch_resumed_at == NOW + timedelta(minutes=90)
    _beat(db_session, row, NOW + timedelta(minutes=91))
    _passes(db_session, client, 91, 120)

    assert job.state is ComputeJobState.RUNNING


def test_a_runner_still_silent_after_krater_is_back_fails_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    _pass(db_session, client, NOW + timedelta(minutes=90))  # back after a long outage

    _passes(db_session, client, 91, 119)
    assert job.state is ComputeJobState.RUNNING
    _pass(db_session, client, NOW + timedelta(minutes=120))  # 30 minutes of silence while Krater watched

    assert job.state is ComputeJobState.FAILED and "stopped reporting" in job.state_reason


def test_a_machine_that_stopped_itself_is_started_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Staging 2026-10-14: Krater went down and a machine billed on for hours. Now its runner stops it after
    `runner_self_stop_minutes` without an answer; Krater, back, starts it again so the job carries on from the
    checkpoints on its disk."""
    job, row = _running_job(db_session, member, project, client, fake)
    _beat(db_session, row, NOW + timedelta(minutes=6))
    # Krater is down; three hours later the runner stops its machine. Vast shows that like an outbid machine.
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=250))

    assert job.state is ComputeJobState.PAUSED and "stopped itself" in job.state_reason
    assert "self_stopped" in _events(db_session, job)
    assert fake.instances[row.provider_instance_id]["intended_status"] == "running"  # Krater started it
    _pass(db_session, client, NOW + timedelta(minutes=251))
    assert job.state is ComputeJobState.RUNNING
    assert row.runner_starts_needed == row.runner_starts + 1  # its runner must report again


def test_a_failed_start_after_a_self_stop_is_retried(db_session, member, project, client, fake, monkeypatch) -> None:  # noqa: F811
    """Vast never resumes a machine stopped on purpose, so one failed start must not leave it stopped for good."""
    job, row = _running_job(db_session, member, project, client, fake)
    _beat(db_session, row, NOW + timedelta(minutes=6))
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")
    real_start = client.start_instance

    def unavailable(instance_id):
        raise ProviderUnavailableError("Vast is down")

    monkeypatch.setattr(client, "start_instance", unavailable)
    _pass(db_session, client, NOW + timedelta(minutes=250))
    assert job.state is ComputeJobState.PAUSED and "self_stopped" in _events(db_session, job)
    assert fake.instances[row.provider_instance_id]["intended_status"] == "stopped"

    monkeypatch.setattr(client, "start_instance", real_start)
    _pass(db_session, client, NOW + timedelta(minutes=251))
    assert fake.instances[row.provider_instance_id]["intended_status"] == "running"
    _pass(db_session, client, NOW + timedelta(minutes=252))
    assert job.state is ComputeJobState.RUNNING and row.start_requested_at is None


def test_an_outbid_machine_is_still_a_pause(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    _beat(db_session, row, NOW + timedelta(minutes=6))
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.PAUSED and "Outbid" in job.state_reason
    assert fake.instances[row.provider_instance_id]["intended_status"] == "stopped"


def test_a_stopped_on_demand_machine_is_not_called_outbid(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Production, 2026-11-13: an on-demand box the provider stopped said "Outbid", which an on-demand machine can't
    be."""
    job, row = _running_job(db_session, member, project, client, fake, interruptible=False)
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.PAUSED
    assert "Outbid" not in job.state_reason and "wasn't outbid" in job.state_reason
    assert row.outbid_count == 0
    assert "The machine was paused (stopped by the provider, not outbid)." in [
        e.message for e in db_session.scalars(sa.select(ComputeEvent).where(ComputeEvent.job_id == job.id))
    ]


def test_out_of_credit_machines_are_kept_and_started_again_once_credit_is_added(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    """Production, 2026-11-13: the Vast account ran out of credit and Vast stopped every machine. Moving one after
    the pause time would have thrown its disk away and couldn't have rented anything."""
    job, row = _running_job(db_session, member, project, client, fake, interruptible=False)
    vast_id = row.provider_instance_id
    waiting = _submit(db_session, member, project, name="waiting")
    fake.account_credit = 0.0
    fake.set_status(vast_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.PAUSED and "ran out of credit" in job.state_reason
    assert row.start_requested_at is not None
    assert waiting.state is ComputeJobState.QUEUED and "out of credit" in waiting.state_reason
    assert compute_watch.latest_credit(db_session).credit_microusd == 0

    _pass(db_session, client, NOW + timedelta(minutes=60))  # long past the 30-minute move

    assert row.state is ComputeInstanceState.PAUSED and vast_id in fake.instances
    assert fake.instances[vast_id]["intended_status"] == "stopped"  # not started while there's no credit
    assert waiting.state is ComputeJobState.QUEUED

    fake.account_credit = 25.0
    _pass(db_session, client, NOW + timedelta(minutes=61))  # credit added: start it again

    assert fake.instances[vast_id]["intended_status"] == "running"
    _pass(db_session, client, NOW + timedelta(minutes=62))
    assert job.state is ComputeJobState.RUNNING and row.state is ComputeInstanceState.RUNNING
    assert row.start_requested_at is None


def test_a_machine_stopped_for_credit_before_it_started_is_not_a_host_failure(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    """Three of those in a row would have failed the job with "Check that the image exists"."""
    job = _submit(db_session, member, project, interruptible=False)
    _pass(db_session, client)
    row = _instance(db_session, job)
    vast_id = row.provider_instance_id
    fake.account_credit = 0.0
    fake.set_status(vast_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert job.state is ComputeJobState.QUEUED and "out of credit" in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED and vast_id not in fake.instances
    kinds = _events(db_session, job)
    assert "credit_out" in kinds and "host_failed" not in kinds


def test_a_failed_credit_check_is_not_out_of_credit(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, interruptible=False)
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/current/"):
            return httpx.Response(500, text="oops")
        return fake.handle(request)

    broken = fake.client()
    broken._http = httpx.Client(transport=httpx.MockTransport(handler))
    _pass(db_session, broken, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.PAUSED and "wasn't outbid" in job.state_reason
    assert row.start_requested_at is None


def test_an_outbid_interruptible_machine_is_still_outbid_with_credit(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.provider_instance_id, "stopped", intended_status="stopped")

    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert "Outbid" in job.state_reason and row.outbid_count == 1 and row.start_requested_at is None


def test_after_a_pause_the_runner_must_start_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.provider_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=7))  # resumed: Vast restarted the container

    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=8))
    _pass(db_session, client, NOW + timedelta(minutes=20))

    assert job.state is ComputeJobState.RUNNING
    assert row.runner_starts == 2


def test_a_runner_that_reports_before_the_scheduler_sees_running_counts(
    db_session, member, project, client, fake
) -> None:  # noqa: F811
    """The usual order: the container starts, the runner reports within seconds, and only the next pass sees Vast's
    `running`."""
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _runner_starts(db_session, fake, row, NOW + timedelta(seconds=30))  # still `provisioning` in Krater

    for minute in range(1, 21):
        if minute % 10 == 0:
            _beat(db_session, row, NOW + timedelta(minutes=minute))
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert job.state is ComputeJobState.RUNNING


def test_a_start_from_before_a_pause_does_not_count_after_it(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.provider_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))  # paused: the runner must start again
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=8))  # resumed, but no new start

    _passes(db_session, client, 9, 17)
    assert job.state is ComputeJobState.RUNNING
    _pass(db_session, client, NOW + timedelta(minutes=18))
    assert row.state is ComputeInstanceState.DESTROYED and row.destroy_reason == scheduler.RUNNER_NOT_STARTED
    assert "runner_not_started" in _events(db_session, job)


def test_a_runner_that_never_starts_is_tried_on_another_machine(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Staging, 2026-11-12: a host's download of the runner was cut short, then hung past the deadline, and the job
    failed on its first machine although nothing was wrong with it."""
    job = _submit(db_session, member, project, target_microusd=100_000)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=1))

    _passes(db_session, client, 2, 10)
    assert row.state is not ComputeInstanceState.DESTROYED
    _pass(db_session, client, NOW + timedelta(minutes=11))

    assert row.state is ComputeInstanceState.DESTROYED and row.destroy_reason == scheduler.RUNNER_NOT_STARTED
    assert row.provider_instance_id not in fake.instances
    assert "runner_not_started" in _events(db_session, job)
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.provider_machine_id != row.provider_machine_id
    assert job.state is ComputeJobState.STARTING


def test_a_runner_that_starts_nowhere_fails_after_three_machines(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, target_microusd=100_000)  # all three RTX 3060 machines qualify
    _pass(db_session, client)
    minute = 0
    for _ in range(scheduler.MAX_HOST_FAILURES):
        fake.set_status(_instance(db_session, job).provider_instance_id, "running")
        _passes(db_session, client, minute + 1, minute + 11)  # a pass a minute: a gap would restart the deadline
        minute += 11

    assert job.state is ComputeJobState.FAILED
    assert "3 machines in a row" in job.state_reason and "POSIX shell" in job.state_reason
    assert fake.instances == {}
    assert len({row.provider_machine_id for row in job.instances}) == scheduler.MAX_HOST_FAILURES


def test_a_machine_whose_runner_didnt_start_is_still_offered_to_other_jobs(
    db_session,  # noqa: F811
    member,  # noqa: F811
    project,  # noqa: F811
    client,  # noqa: F811
    fake,  # noqa: F811
) -> None:
    """An image without a shell or a downloader fails this way on any machine: that says nothing about the host."""
    job = _submit(db_session, member, project, target_microusd=100_000)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _passes(db_session, client, 1, 11)
    assert row.destroy_reason == scheduler.RUNNER_NOT_STARTED and row.provider_machine_id == "501"

    other = _submit(db_session, member, project, now=NOW + timedelta(minutes=12), target_microusd=100_000)
    _pass(db_session, client, NOW + timedelta(minutes=12))

    assert _instance(db_session, other).provider_machine_id == "501"  # still the cheapest, and not held back


def test_a_runner_restart_reported_before_the_resume_is_seen_counts(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.provider_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))
    fake.set_status(row.provider_instance_id, "running")
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=7))  # before the pass that sees the resume

    for minute in range(8, 25):
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert job.state is ComputeJobState.RUNNING


def test_no_runner_binary_means_nothing_is_rented(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    scheduler.run_pass(db_session, client, SETTINGS, now=NOW, runner_sha256=None, weave_client=current_weave[0])

    assert job.state is ComputeJobState.QUEUED
    assert fake.instances == {}


def test_a_limit_order_speaks_of_its_limit(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session,
        member,
        project,
        target_microusd=10_000,
        max_microusd=10_000,
        on_outbid=ComputeOutbid.HOLD,
        max_wait_minutes=120,
        on_max_wait=ComputeEscalation.CANCEL,
    )
    assert job.state_reason == "Waiting for an offer at or under its limit."

    _pass(db_session, client, NOW + timedelta(hours=2))

    db_session.refresh(job)
    assert job.state is ComputeJobState.CANCELLED
    assert job.state_reason == "No offer met the limit within 2 hours."
