"""Production config guards: `Settings._validate_production_safety`.

Each case builds a `Settings` that is otherwise "production ready" (every live mode on, every secret
filled in, https `base_url`, a long `secret_key`) and knocks out exactly one requirement, to prove that
one -- and only that one -- is what's being checked.
"""

from __future__ import annotations

import pytest

from krater.config import MIN_SECRET_KEY_LENGTH, Settings

_LONG_SECRET_KEY = "x" * MIN_SECRET_KEY_LENGTH

_PRODUCTION_READY: dict = {
    "env": "production",
    "secret_key": _LONG_SECRET_KEY,
    "base_url": "https://ganymede.patchworklabs.example",
    "weave_mode": "live",
    "weave_client_secret": "weave-client-secret",
    "pricing_mode": "live",
    "slack_mode": "live",
    "slack_signing_secret": "slack-signing-secret",
    "s3_mode": "live",
    "s3_access_key_id": "s3-access-key",
    "s3_secret_access_key": "s3-secret-key",
}


def test_production_ready_settings_construct_fine() -> None:
    Settings(**_PRODUCTION_READY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"weave_mode": "stub"},
        {"pricing_mode": "fake"},
        {"slack_mode": "fake"},
        {"s3_mode": "fake"},
    ],
    ids=["weave-stub", "pricing-fake", "slack-fake", "s3-fake"],
)
def test_production_rejects_dev_modes(overrides: dict) -> None:
    with pytest.raises(ValueError):
        Settings(**{**_PRODUCTION_READY, **overrides})


def test_production_with_compute_off_still_refuses_the_fake_price_catalog() -> None:
    """The budget estimator's GPU prices come from the public catalog even with compute off."""
    with pytest.raises(ValueError, match="PRICING_MODE"):
        Settings(**{**_PRODUCTION_READY, "compute_mode": "off", "pricing_mode": "fake"})


def test_renamed_settings_still_read_their_old_env_names(monkeypatch) -> None:
    """A deploy can update Krater before its env file: the SKYPILOT_ names keep working for one release."""
    monkeypatch.setenv("KRATER_SKYPILOT_MODE", "live")
    monkeypatch.setenv("KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS", "750")
    monkeypatch.setenv("KRATER_SKYPILOT_BUDGET_WARN_PERCENT", "70")

    settings = Settings()

    assert (settings.pricing_mode, settings.compute_max_hourly_cost_cents, settings.budget_warn_percent) == (
        "live",
        750,
        70,
    )
    monkeypatch.setenv("KRATER_COMPUTE_MAX_HOURLY_COST_CENTS", "900")
    assert Settings().compute_max_hourly_cost_cents == 900  # the new name wins


def test_production_compute_on_vast_needs_the_live_vast_api() -> None:
    with pytest.raises(ValueError, match="VAST_MODE"):
        Settings(**{**_PRODUCTION_READY, "compute_mode": "vast", "vast_mode": "fake"})

    with pytest.raises(ValueError, match="LABEL_PREFIX"):
        Settings(**{**_PRODUCTION_READY, "compute_mode": "vast", "vast_mode": "live"})
    Settings(
        **{**_PRODUCTION_READY, "compute_mode": "vast", "vast_mode": "live", "compute_label_prefix": "krater-prod-"}
    )


@pytest.mark.parametrize("prefix", ["", "-", "krater", "krater-", "krater--", "kr-", "krater-prod"])
def test_a_label_prefix_must_be_specific(prefix: str) -> None:
    """The scheduler destroys unknown instances with its prefix: a vague one would match other tools' machines."""
    with pytest.raises(ValueError, match="LABEL_PREFIX"):
        Settings(compute_label_prefix=prefix)


def test_production_rejects_the_default_secret_key() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(**{**_PRODUCTION_READY, "secret_key": "insecure-dev-secret-change-me"})


def test_production_rejects_a_long_placeholder_secret_key() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(**{**_PRODUCTION_READY, "secret_key": "change-me-in-production-" + "x" * 40})


def test_production_rejects_a_short_secret_key() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(**{**_PRODUCTION_READY, "secret_key": "x" * (MIN_SECRET_KEY_LENGTH - 1)})


def test_production_accepts_a_secret_key_at_exactly_the_minimum_length() -> None:
    Settings(**{**_PRODUCTION_READY, "secret_key": "x" * MIN_SECRET_KEY_LENGTH})


@pytest.mark.parametrize("base_url", ["http://ganymede.example", "ganymede.example", ""])
def test_production_rejects_a_non_https_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="BASE_URL"):
        Settings(**{**_PRODUCTION_READY, "base_url": base_url})


def test_production_accepts_an_https_base_url() -> None:
    Settings(**{**_PRODUCTION_READY, "base_url": "https://ganymede.example"})


@pytest.mark.parametrize(
    "field,env_name",
    [
        ("weave_client_secret", "WEAVE_CLIENT_SECRET"),
        ("slack_signing_secret", "SLACK_SIGNING_SECRET"),
        ("s3_access_key_id", "S3_ACCESS_KEY_ID"),
        ("s3_secret_access_key", "S3_SECRET_ACCESS_KEY"),
    ],
)
def test_production_requires_each_live_mode_secret(field: str, env_name: str) -> None:
    with pytest.raises(ValueError, match=env_name):
        Settings(**{**_PRODUCTION_READY, field: ""})


def test_development_and_test_envs_are_unconstrained() -> None:
    # None of the production guards apply outside production -- dev/test can run with every mode fake
    # and the default secret key, which is exactly what most of this test suite already relies on.
    Settings(env="development")
    Settings(env="test")


def test_weave_role_keys_and_group_slugs_have_the_contract_defaults() -> None:
    settings = Settings()

    assert (settings.weave_role_member, settings.weave_role_reviewer, settings.weave_role_admin) == (
        "member",
        "reviewer",
        "admin",
    )
    assert (settings.weave_group_member, settings.weave_group_reviewer, settings.weave_group_admin) == (
        "ganymede-members",
        "krater-reviewers",
        "krater-admins",
    )


def test_weave_role_keys_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("KRATER_WEAVE_ROLE_REVIEWER", "krater-reviewer")
    monkeypatch.setenv("KRATER_WEAVE_API_BASE_URL", "https://api.weave.test")

    settings = Settings()

    assert settings.weave_role_reviewer == "krater-reviewer"
    assert settings.weave_api_base_url == "https://api.weave.test"


def test_bootstrap_admins_setting_is_gone() -> None:
    assert "bootstrap_admins" not in Settings.model_fields


def test_production_accepts_a_blank_or_https_quilt_url() -> None:
    Settings(**_PRODUCTION_READY, quilt_url="")
    Settings(**_PRODUCTION_READY, quilt_url="https://quilt.patchworklabs.example")


def test_production_rejects_a_plain_http_quilt_url() -> None:
    with pytest.raises(ValueError, match="KRATER_QUILT_URL"):
        Settings(**_PRODUCTION_READY, quilt_url="http://quilt.patchworklabs.example")


def test_a_final_warning_at_or_below_the_first_turns_the_final_warning_off() -> None:
    assert Settings().budget_warn_percents == (80, 95)
    assert Settings(budget_warn_percent=95).budget_warn_percents == (95,)
