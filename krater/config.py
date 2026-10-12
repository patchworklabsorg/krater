"""Application settings, loaded from environment variables prefixed ``KRATER_``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Sentinel used as the default `secret_key`. Production must override it (and, either way, production
#: requires at least `MIN_SECRET_KEY_LENGTH` characters -- see `_validate_production_safety`).
DEFAULT_SECRET_KEY = "insecure-dev-secret-change-me"
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Krater's configuration. All fields read from `KRATER_<FIELD_NAME>` env vars (or a `.env` file)."""

    # `populate_by_name`: fields renamed with an alias for their old env name still take their new name in code.
    model_config = SettingsConfigDict(env_prefix="KRATER_", env_file=".env", extra="ignore", populate_by_name=True)

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://root:root@localhost:5432/krater_dev"
    secret_key: str = DEFAULT_SECRET_KEY
    base_url: str = "http://localhost:8000"
    session_cookie_max_age_seconds: int = 60 * 60 * 24 * 14  # 14 days

    # Trusted reverse proxies in front of this app (e.g. an nginx/ALB terminating TLS). Each hop is
    # expected to append the client's address to `X-Forwarded-For` -- this is how many trailing hops of
    # that header the app trusts as having been added by infrastructure it controls, so it can pick out
    # the real client IP for rate limiting instead of blindly trusting a header any client can forge.
    # 0 (the default) means: don't trust `X-Forwarded-For` at all, use the socket peer address.
    trusted_proxy_count: int = 0

    # Weave (Patchwork Labs identity provider): sign-in, roles and the directory API. See
    # docs/weave-integration.md.
    weave_mode: Literal["stub", "live"] = "stub"
    weave_issuer: str = ""
    weave_client_id: str = ""
    weave_client_secret: str = ""
    # Base URL of Weave's directory API (`/api/v1/directory/...`). Blank means `weave_issuer`.
    weave_api_base_url: str = ""
    weave_stub_users_file: str = ""

    # How Weave's answers map onto Krater's roles (`ganymede:member`, `ganymede:reviewer`,
    # `ganymede:admin`). The `roles` claim (Krater's app-defined role keys in Weave) is the source of
    # truth. Only when Weave sends no `roles` at all does Krater fall back to these group slugs.
    # See `krater.weave.roles`.
    weave_role_member: str = "member"
    weave_role_reviewer: str = "reviewer"
    weave_role_admin: str = "admin"
    weave_group_member: str = "ganymede-members"
    weave_group_reviewer: str = "krater-reviewers"
    weave_group_admin: str = "krater-admins"

    # Which compute layer launches members' machines. Only `off` for now: Krater runs proposals, review, budgets,
    # Slack, the gallery and Quilt events, and launches nothing. Krater Compute (its own Vast layer) adds `vast`.
    compute_mode: Literal["off"] = "off"

    # Budgets and caps, whatever compute layer runs. The old SKYPILOT_ env names still work for one release, so a
    # deploy doesn't have to change its env file and Krater in the same step.
    budget_warn_percent: int = Field(
        80, validation_alias=AliasChoices("KRATER_BUDGET_WARN_PERCENT", "KRATER_SKYPILOT_BUDGET_WARN_PERCENT")
    )
    # The default hourly price cap per machine; a project can have its own.
    compute_max_hourly_cost_cents: int = Field(
        500,
        validation_alias=AliasChoices("KRATER_COMPUTE_MAX_HOURLY_COST_CENTS", "KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS"),
    )

    # The public GPU price catalog behind /pricing and the budget estimator (krater.pricing.catalog,
    # docs/dev/pricing.md): SkyPilot's Vast catalog CSV, fetched from GitHub with no server or credentials. `fake`
    # serves a canned catalog; production refuses it. KRATER_SKYPILOT_MODE used to choose this, and still does.
    pricing_mode: Literal["fake", "live"] = Field(
        "fake", validation_alias=AliasChoices("KRATER_PRICING_MODE", "KRATER_SKYPILOT_MODE")
    )
    pricing_catalog_url: str = Field(
        "https://raw.githubusercontent.com/skypilot-org/skypilot-catalog/master/catalogs/v8/vast/vms.csv",
        validation_alias=AliasChoices("KRATER_PRICING_CATALOG_URL", "KRATER_SKYPILOT_CATALOG_URL"),
    )
    pricing_catalog_fetch_timeout_seconds: float = Field(
        15.0,
        validation_alias=AliasChoices(
            "KRATER_PRICING_CATALOG_FETCH_TIMEOUT_SECONDS", "KRATER_SKYPILOT_CATALOG_FETCH_TIMEOUT_SECONDS"
        ),
    )
    pricing_refresh_cron: str = "0 7 * * *"  # daily, off-peak UTC -- see docs/dev/pricing.md
    budget_estimate_default_margin_percent: int = 20
    # How far a requested budget can differ from its stored estimate (as a percent of the estimate)
    # before the project page flags the mismatch to reviewers.
    budget_estimate_flag_threshold_percent: int = 20

    # Slack integration (review happens in Slack; see docs/SPEC.md "Slack integration" and
    # docs/dev/slack-setup.md). `fake` is an in-memory Slack for dev and tests, mirroring `vast_mode`.
    slack_mode: Literal["fake", "live"] = "fake"
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_feed_channel_id: str = ""
    slack_reconcile_interval_minutes: int = 10

    # S3-compatible object storage (gallery screenshots). docker-compose runs a temporary SeaweedFS as `storage`.
    # `fake` uses an in-memory store for dev and tests; `live` talks to a real S3-compatible bucket. See
    # docs/dev/storage.md.
    s3_mode: Literal["fake", "live"] = "fake"
    s3_endpoint_url: str = ""
    s3_public_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "krater-screenshots"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Quilt (Patchwork Labs finance): Krater sends submission and budget events to Quilt's patch API. See
    # docs/quilt-integration.md. Blank means "don't send": the services still write every event to the
    # `quilt_outbox` table, so nothing is lost, and the sender sends the backlog once this is set.
    quilt_url: str = ""
    quilt_timeout_seconds: float = 10.0
    # The most outbox rows one sender run sends. The periodic task runs every minute.
    quilt_batch_size: int = 200

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Settings:
        if self.env == "production":
            if self.weave_mode == "stub":
                raise ValueError("KRATER_WEAVE_MODE cannot be 'stub' when KRATER_ENV=production")
            if self.pricing_mode == "fake":
                # Even with compute off: the budget estimator's GPU prices come from the public catalog.
                raise ValueError("KRATER_PRICING_MODE cannot be 'fake' when KRATER_ENV=production")
            if self.slack_mode == "fake":
                raise ValueError("KRATER_SLACK_MODE cannot be 'fake' when KRATER_ENV=production")
            if self.s3_mode == "fake":
                raise ValueError("KRATER_S3_MODE cannot be 'fake' when KRATER_ENV=production")
            # Also refuse anything that still looks like a placeholder (e.g. a lengthened copy of .env.example's).
            if (
                len(self.secret_key) < MIN_SECRET_KEY_LENGTH
                or self.secret_key == DEFAULT_SECRET_KEY
                or "change-me" in self.secret_key.lower()
            ):
                raise ValueError(
                    f"KRATER_SECRET_KEY must be set to a non-default value of at least "
                    f"{MIN_SECRET_KEY_LENGTH} characters when KRATER_ENV=production"
                )
            if self.quilt_url and not self.quilt_url.startswith("https://"):
                raise ValueError("KRATER_QUILT_URL must be an https:// URL when KRATER_ENV=production")
            if not self.base_url.startswith("https://"):
                raise ValueError("KRATER_BASE_URL must be an https:// URL when KRATER_ENV=production")
            # The live modes above are mandatory in production, so their secrets must actually be set --
            # an empty value would otherwise pass every mode check above and fail confusingly later
            # (an unauthenticated Weave/Slack/S3 client, or one Weave rejects) instead of at startup.
            if not self.weave_client_secret:
                raise ValueError("KRATER_WEAVE_CLIENT_SECRET must be set when KRATER_ENV=production")
            if not self.slack_signing_secret:
                raise ValueError("KRATER_SLACK_SIGNING_SECRET must be set when KRATER_ENV=production")
            if not self.s3_access_key_id:
                raise ValueError("KRATER_S3_ACCESS_KEY_ID must be set when KRATER_ENV=production")
            if not self.s3_secret_access_key:
                raise ValueError("KRATER_S3_SECRET_ACCESS_KEY must be set when KRATER_ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` instance, built once and cached."""
    return Settings()
