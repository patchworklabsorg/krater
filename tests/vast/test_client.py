"""`VastClient` against `FakeVast`: every call, and the live-API facts the client must follow."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from krater.config import Settings
from krater.vast import (
    CreateInstance,
    FakeVast,
    OfferQuery,
    VastClient,
    VastRateLimitedError,
    VastRedirectError,
    VastRequestFailedError,
    VastSearchQuotaError,
    VastUnavailableError,
    build_vast_client,
    usd_to_microusd,
)
from krater.vast.fake import API_HOST


@pytest.fixture
def fake() -> FakeVast:
    return FakeVast()


@pytest.fixture
def client(fake: FakeVast) -> VastClient:
    return fake.client()


def _create(client: VastClient, offer_id: str = "1001", **kwargs) -> str:
    request = CreateInstance(image="ghcr.io/alice/train:v3", disk_gb=32, label="krater-test", **kwargs)
    return client.create_instance(offer_id, request)


# -- Search ------------------------------------------------------------------------------------------


def test_a_search_records_vasts_daily_row_quota(client: VastClient, fake: FakeVast) -> None:
    fake.set_search_quota(20_000, remaining=500, reset_at=1_791_763_200)
    assert client.search_quota is None

    found = client.search_offers(OfferQuery(gpu_names=("RTX 3060",), limit=64))

    quota = client.search_quota
    assert quota.limit == 20_000
    assert quota.remaining == 500 - len(found)
    assert quota.reset_at == datetime(2026, 10, 12, tzinfo=UTC)


def test_a_used_up_quota_raises_its_own_error_with_the_reset(client: VastClient, fake: FakeVast) -> None:
    # The live answer (staging, 2026-10-11): 429 {"error": "search_quota_exceeded", "msg": "Daily search row quota
    # exhausted", "retry_after": ..., "limit": 20000, "remaining": 0} with x-ratelimit-* headers.
    fake.set_search_quota(20_000, remaining=0, reset_at=1_791_763_200)

    with pytest.raises(VastSearchQuotaError) as raised:
        client.search_offers(OfferQuery(gpu_names=("RTX 3060",)))

    assert raised.value.reset_at == datetime(2026, 10, 12, tzinfo=UTC)
    assert client.search_quota.exhausted(datetime(2026, 10, 11, 18, tzinfo=UTC))
    assert not client.search_quota.exhausted(datetime(2026, 10, 12, 0, 1, tzinfo=UTC))


def test_a_plain_429_is_still_a_rate_limit(client: VastClient, fake: FakeVast) -> None:
    fake.fail_next(429)

    with pytest.raises(VastRateLimitedError) as raised:
        client.search_offers(OfferQuery(gpu_names=("RTX 3060",)))

    assert not isinstance(raised.value, VastSearchQuotaError)


def test_search_sends_the_job_disk_and_sorts_by_full_cost(client: VastClient, fake: FakeVast) -> None:
    offers = client.search_offers(OfferQuery(rental_type="bid", gpu_names=("RTX 3060",), disk_gb=32))

    body = fake.search_bodies[-1]
    assert body["allocated_storage"] == 32
    assert body["disk_space"] == {"gte": 32}
    assert body["gpu_name"] == {"eq": "RTX 3060"}
    assert body["type"] == "bid"
    assert body["reliability"] == {"gte": 0.95}
    # dph_total at 32 GB: 1003 (cheap disk) beats 1002 (cheap GPU, expensive disk).
    assert [o.id for o in offers] == ["1001", "1003", "1002"]
    assert [o.dph_total for o in offers] == sorted(o.dph_total for o in offers)
    assert all(o.gpu_name == "RTX 3060" for o in offers)


def test_offer_prices_are_decimals_and_full_cost_counts_disk(client: VastClient) -> None:
    offer = next(o for o in client.search_offers(OfferQuery(gpu_names=("RTX 3060",), disk_gb=32)) if o.id == "1002")

    assert offer.min_bid == Decimal("0.045")
    assert offer.dph_base == Decimal("0.07")
    # min_bid is the GPU alone; disk at $0.80/GB-month for 32 GB adds 0.8 * 32 / 730 per hour.
    expected = Decimal("0.045") + Decimal("0.8") * 32 / 730
    assert offer.full_hourly("bid", 32) == expected
    assert offer.full_hourly("bid", 32, bid=Decimal("0.05")) == Decimal("0.05") + Decimal("0.8") * 32 / 730
    assert offer.full_hourly("ondemand", 32) == Decimal("0.07") + Decimal("0.8") * 32 / 730


def test_search_with_several_models_and_min_vram(client: VastClient, fake: FakeVast) -> None:
    client.search_offers(OfferQuery(gpu_names=("RTX 3060", "RTX 4090"), min_gpu_ram_gb=24, min_direct_ports=1))

    body = fake.search_bodies[-1]
    assert body["gpu_name"] == {"in": ["RTX 3060", "RTX 4090"]}
    assert body["gpu_ram"] == {"gte": 24_000}
    assert body["direct_port_count"] == {"gte": 1}


def test_search_never_asks_for_more_than_vasts_cap(client: VastClient, fake: FakeVast) -> None:
    client.search_offers(OfferQuery(limit=5000))

    assert fake.search_bodies[-1]["limit"] == 512


# -- Instances ---------------------------------------------------------------------------------------


def test_create_returns_only_the_id_and_drops_the_instance_key(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client, bid_price=Decimal("0.055"), env={"HF_TOKEN": "hf_secret value"}, ports=(22,))

    assert isinstance(instance_id, str) and instance_id.isdigit()
    assert fake.instances[instance_id]["label"] == "krater-test"
    # Env values are shell-quoted Docker flags.
    assert fake.instances[instance_id]["env"] == "-e 'HF_TOKEN=hf_secret value' -p 22:22"
    assert fake.instances[instance_id]["is_bid"] is True


def test_create_instance_repr_hides_env_values() -> None:
    request = CreateInstance(image="x", disk_gb=10, label="l", env={"HF_TOKEN": "hf_secret"})

    assert "hf_secret" not in repr(request)
    assert "HF_TOKEN" in repr(request)


def test_create_refuses_an_overlong_onstart_and_bad_env_names() -> None:
    with pytest.raises(ValueError, match="onstart"):
        CreateInstance(image="x", disk_gb=10, label="l", onstart="x" * 4049)
    with pytest.raises(ValueError, match="environment variable"):
        CreateInstance(image="x", disk_gb=10, label="l", env={"BAD NAME": "v"})


def test_create_on_a_rented_offer_is_refused(client: VastClient) -> None:
    _create(client)

    with pytest.raises(VastRequestFailedError) as excinfo:
        _create(client)
    assert excinfo.value.error == "invalid_args"


def test_create_failure_messages_never_carry_the_key(client: VastClient, fake: FakeVast) -> None:
    with pytest.raises(VastRequestFailedError) as excinfo:
        _create(client, bid_price=Decimal("0.001"))
    assert fake.api_key not in str(excinfo.value)


def test_show_list_and_unknown_ids(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client)
    fake.set_status(instance_id, "running", gpu_util=99.9)

    shown = client.show_instance(instance_id)
    assert shown is not None and shown.is_running and shown.gpu_util == pytest.approx(99.9)
    assert shown.machine_id == "501"
    assert client.show_instance(1) is None
    assert [i.id for i in client.list_instances()] == [instance_id]
    assert client.list_instances(label_prefix="other-") == []


def test_list_follows_every_page(client: VastClient, fake: FakeVast) -> None:
    for n in range(30):
        fake.add_offer(9000 + n, 9000 + n, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
        _create(client, 9000 + n)

    assert len(client.list_instances()) == 30
    assert sum(1 for method, path in fake.requests if path == "/api/v1/instances/") == 2


def test_change_bid_stop_start_destroy(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client, bid_price=Decimal("0.05"))

    client.change_bid(instance_id, Decimal("0.064"))
    assert fake.instances[instance_id]["dph_base"] == pytest.approx(0.064)
    client.stop_instance(instance_id)
    assert fake.instances[instance_id]["intended_status"] == "stopped"
    client.start_instance(instance_id)
    assert fake.instances[instance_id]["intended_status"] == "running"
    assert client.destroy_instance(instance_id) is True
    assert client.show_instance(instance_id) is None
    assert client.destroy_instance(instance_id) is False


def test_every_path_has_its_trailing_slash(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client)
    client.show_instance(instance_id)
    client.list_instances()
    client.change_bid(instance_id, Decimal("0.07"))
    client.stop_instance(instance_id)
    client.request_logs(instance_id)
    client.charges(datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC))
    client.destroy_instance(instance_id)

    api_paths = [path for method, path in fake.requests if not path.startswith("https://")]
    assert api_paths and all(path.endswith("/") for path in api_paths)


def test_a_redirect_is_an_error_and_is_never_followed(fake: FakeVast) -> None:
    """What a missing trailing slash looks like on the live API: 308, and the DELETE did nothing."""
    instance_id = _create(fake.client())
    raw = httpx.Client(transport=fake.transport())
    response = raw.delete(
        f"https://{API_HOST}/api/v0/instances/{instance_id}", headers={"Authorization": f"Bearer {fake.api_key}"}
    )
    assert response.status_code == 308
    assert instance_id in fake.instances

    client = fake.client()
    with pytest.raises(ValueError, match="slash"):
        client._request("DELETE", f"/api/v0/instances/{instance_id}")

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(308, headers={"Location": "https://console.vast.ai/elsewhere/"})

    redirecting = VastClient("k", http_client=httpx.Client(transport=httpx.MockTransport(redirect)))
    with pytest.raises(VastRedirectError):
        redirecting.destroy_instance(instance_id)


def test_the_v0_instance_list_is_gone(fake: FakeVast) -> None:
    client = fake.client()
    with pytest.raises(VastRequestFailedError) as excinfo:
        client._request("GET", "/api/v0/instances/")
    assert excinfo.value.status_code == 410


def test_a_wrong_key_is_refused(fake: FakeVast) -> None:
    wrong = VastClient("not-the-key", http_client=httpx.Client(transport=fake.transport()))

    with pytest.raises(VastRequestFailedError) as excinfo:
        wrong.list_instances()
    assert excinfo.value.status_code == 401


def test_rate_limits_and_server_errors_are_retryable(client: VastClient, fake: FakeVast) -> None:
    fake.fail_next(429)
    with pytest.raises(VastRateLimitedError):
        client.list_instances()
    fake.fail_next(502)
    with pytest.raises(VastUnavailableError):
        client.list_instances()
    assert client.list_instances() == []


def test_a_network_error_is_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = VastClient("k", http_client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(VastUnavailableError):
        client.list_instances()


# -- Logs and charges --------------------------------------------------------------------------------


def test_logs_are_fetched_without_the_org_key(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client)
    fake.logs[instance_id] = "step 1\nstep 2\n"

    assert client.request_logs(instance_id) == "step 1\nstep 2\n"
    assert fake.log_fetch_saw_authorization is False


def test_logs_retry_until_written_then_give_up_empty(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client)
    sleeps: list[float] = []
    client._sleep = sleeps.append

    assert client.request_logs(instance_id, attempts=3, wait_seconds=2.0) == ""
    assert sleeps == [2.0, 2.0]


def test_charges_follow_every_page_and_map_to_instances(client: VastClient, fake: FakeVast) -> None:
    instance_id = _create(client)
    for _ in range(501):
        fake.add_charge(instance_id, gpu="0.011", disk="0.001")

    charges = client.charges(datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC))

    assert len(charges) == 501
    assert charges[0].instance_id == instance_id
    assert charges[0].amount == Decimal("0.012")
    assert [item.type for item in charges[0].items] == ["gpu", "disk"]
    assert charges[0].label == "krater-test"


# -- Factory and units -------------------------------------------------------------------------------


def test_build_vast_client_modes(tmp_path) -> None:
    assert isinstance(build_vast_client(Settings(vast_mode="fake")), VastClient)
    assert build_vast_client(Settings(vast_mode="live")) is None
    key_file = tmp_path / "vast_api_key"
    key_file.write_text("secret-key\n", encoding="utf-8")
    live = build_vast_client(Settings(vast_mode="live", vast_api_key_file=str(key_file)))
    assert isinstance(live, VastClient)
    assert "secret-key" not in repr(live)
    assert build_vast_client(Settings(vast_mode="live", vast_api_key_file=str(tmp_path / "missing"))) is None


def test_usd_to_microusd_rounds_half_up() -> None:
    assert usd_to_microusd(Decimal("0.0587")) == 58_700
    assert usd_to_microusd(Decimal("0.0000005")) == 1
    assert usd_to_microusd(Decimal("0.05888888888888889")) == 58_889


def test_get_vast_client_picks_up_a_new_key_without_a_restart(tmp_path, monkeypatch) -> None:
    import krater.vast as vast_package
    from krater.config import get_settings

    key_file = tmp_path / "vast_api_key"
    monkeypatch.setattr(vast_package, "_cached", None)
    monkeypatch.setattr(get_settings(), "vast_mode", "live")
    monkeypatch.setattr(get_settings(), "vast_api_key_file", str(key_file))

    # Asserted on the logger itself: the app's logging setup (other tests) can stop caplog seeing krater.* records.
    with patch.object(vast_package.logger, "error") as log_error:
        assert vast_package.get_vast_client() is None
    assert "no Vast API key" in log_error.call_args.args[0]

    key_file.write_text("first-key", encoding="utf-8")
    first = vast_package.get_vast_client()
    assert first is not None and first is vast_package.get_vast_client()

    key_file.write_text("rotated-key", encoding="utf-8")
    second = vast_package.get_vast_client()
    assert second is not None and second is not first


@pytest.mark.parametrize("bad", ["", "12a", "../1", "1/2", "-1", " 1", "１２", "1" * 20, "i-0abc"])
def test_only_plain_digit_ids_reach_a_vast_path(client: VastClient, fake: FakeVast, bad: str) -> None:
    """Krater keeps every provider's ids as strings; one that isn't Vast's must never shape a request path."""
    with pytest.raises(ValueError, match="not a Vast id"):
        client.show_instance(bad)
    assert fake.requests == []
