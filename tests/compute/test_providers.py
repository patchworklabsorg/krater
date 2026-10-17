"""The provider seam (`krater.providers`): the registry, neutral errors, and Krater offering only what the provider in
use can do. Vast is the only provider; the others here are Vast with capabilities taken away."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from krater import providers
from krater.compute import runner_api
from krater.config import Settings, get_settings
from krater.models import ComputeEscalation, ComputeJobKind, ComputeJobState, ComputeOutbid
from krater.services.errors import ValidationFailed
from krater.vast.errors import (
    VastRateLimitedError,
    VastRedirectError,
    VastRequestFailedError,
    VastSearchQuotaError,
    VastUnavailableError,
)
from tests.compute.test_scheduler import NOW, _instance, _pass, _runner_starts, _submit, _token


@pytest.fixture
def without(monkeypatch):
    """Make the active provider Vast without the given capabilities."""

    def take_away(**flags: bool) -> None:
        monkeypatch.setattr(get_settings(), "compute_mode", "vast")
        vast = providers.registered()["vast"]
        changed = {name: False for name in flags}
        monkeypatch.setitem(
            providers._REGISTRY,
            "vast",
            dataclasses.replace(
                vast,
                capabilities=dataclasses.replace(vast.capabilities, **changed),
                self_stop_method=None if "self_stop" in flags else vast.self_stop_method,
            ),
        )

    return take_away


def test_compute_mode_picks_the_provider() -> None:
    assert providers.active(Settings(compute_mode="off")) is None
    assert providers.active(Settings(compute_mode="vast")).title == "Vast.ai"
    assert providers.get_provider(Settings(compute_mode="off")) is None


def test_off_is_not_a_provider_name() -> None:
    info = providers.registered()["vast"]
    with pytest.raises(ValueError):
        providers.register(dataclasses.replace(info, name="off"))


def test_vast_can_do_everything_krater_offers() -> None:
    can = providers.registered()["vast"].capabilities
    assert all(dataclasses.asdict(can).values()) and can.boxes


def test_vasts_errors_are_provider_errors() -> None:
    assert issubclass(VastUnavailableError, providers.ProviderUnavailableError)
    assert issubclass(VastRateLimitedError, providers.ProviderRateLimitedError)
    assert issubclass(VastSearchQuotaError, providers.ProviderSearchQuotaError)
    assert issubclass(VastRequestFailedError, providers.ProviderRequestFailedError)
    # A create Vast answered 200 without success may have rented anyway; a redirect did nothing.
    assert VastRequestFailedError("x", status_code=200).may_have_happened
    assert not VastRequestFailedError("x", status_code=400).may_have_happened
    assert not VastRedirectError("x", status_code=200).may_have_happened


def test_a_provider_without_ssh_runs_no_boxes(db_session, member, project, without) -> None:  # noqa: F811
    without(ssh=True)
    with pytest.raises(ValidationFailed) as refused:
        _submit(db_session, member, project, kind=ComputeJobKind.BOX, command=None)
    assert refused.value.errors["kind"] == "Vast.ai machines can't be boxes: run a job instead."


def test_a_provider_that_sets_its_own_prices_takes_no_limit_orders(db_session, member, project, without) -> None:  # noqa: F811
    without(bids=True)
    with pytest.raises(ValidationFailed) as refused:
        _submit(
            db_session,
            member,
            project,
            target_microusd=70_000,
            max_microusd=70_000,
            on_outbid=ComputeOutbid.HOLD,
            on_max_wait=ComputeEscalation.RAISE_BID,
        )
    assert set(refused.value.errors) == {"on_outbid", "on_max_wait"}


def test_on_demand_only_and_interruptible_only_providers(db_session, member, project, without) -> None:  # noqa: F811
    without(interruptible=True)
    with pytest.raises(ValidationFailed) as refused:
        _submit(db_session, member, project)
    assert refused.value.errors["interruptible"] == "Vast.ai has no interruptible machines."
    _submit(db_session, member, project, interruptible=False)  # on demand is fine


def test_a_provider_that_can_do_it_all_refuses_nothing(db_session, member, project, without) -> None:  # noqa: F811
    without()
    _submit(db_session, member, project, kind=ComputeJobKind.BOX, command=None)


def test_rentals_record_their_provider(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    assert _instance(db_session, job).provider == "vast"


def test_a_pass_leaves_another_providers_machines_alone(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    row.provider = "elsewhere"
    db_session.commit()
    fake.set_status(row.provider_instance_id, "running")

    _pass(db_session, client, NOW + timedelta(minutes=5))

    db_session.refresh(job)
    assert job.state is ComputeJobState.STARTING  # not watched by Vast's pass
    assert row.provider_instance_id in fake.instances  # nor swept: its label is live


def test_the_runner_keeps_its_switch_off_where_the_provider_cant_stop_it(
    db_session, member, project, client, fake, without
) -> None:  # noqa: E501, F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    without(self_stop=True)

    info = runner_api.start(
        db_session, runner_api.authenticate(db_session, _token(fake, row)), now=NOW, settings=get_settings()
    )

    assert info.self_stop_after_seconds == -1


def test_the_runner_gets_the_policys_switch_where_the_provider_can(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    _runner_starts(db_session, fake, _instance(db_session, job), NOW)
    info = runner_api.start(
        db_session,
        runner_api.authenticate(db_session, _token(fake, _instance(db_session, job))),
        now=NOW,
        settings=Settings(compute_mode="vast"),
    )
    assert info.self_stop_after_seconds == 180 * 60


def test_a_self_stopping_provider_must_name_how() -> None:
    """Without a method a runner would fall back to Vast's API, which is the wrong call for any other provider."""
    info = providers.registered()["vast"]
    with pytest.raises(ValueError, match="self_stop_method"):
        dataclasses.replace(info, name="other", self_stop_method=None)
    with pytest.raises(ValueError, match="self_stop_method"):
        dataclasses.replace(info, name="other", capabilities=dataclasses.replace(info.capabilities, self_stop=False))
