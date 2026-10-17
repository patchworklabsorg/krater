"""Application settings, loaded from environment variables prefixed ``KRATER_``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Sentinel used as the default `secret_key`. Production must override it (and, either way, production
#: requires at least `MIN_SECRET_KEY_LENGTH` characters -- see `_validate_production_safety`).
DEFAULT_SECRET_KEY = "insecure-dev-secret-change-me"
MIN_SECRET_KEY_LENGTH = 32
DEFAULT_COMPUTE_LABEL_PREFIX = "krater-job-"


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
    # The Krater CLI's own Weave app: a public (non-confidential) OAuth app the `krater` command signs in with
    # (docs/compute/cli.md). Blank turns the CLI's API (`/api/v1/`) off.
    weave_cli_client_id: str = ""
    # Whether that app accepts a loopback redirect (`http://127.0.0.1/callback`, any port). Production Weave refuses
    # plain-http redirect URIs, so the CLI's default is copy-and-paste (`urn:ietf:wg:oauth:2.0:oob`).
    weave_cli_loopback: bool = False

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

    # Which provider Krater Compute rents members' machines from (`krater.providers`; a new provider adds its name
    # here). `vast`: the worker rents Vast machines itself (`krater.compute`, docs/compute/). `off` launches nothing:
    # Krater still runs proposals, review, budgets, Slack, the gallery and Quilt events. Off by default, so a developer
    # with a live provider key can't rent by accident.
    compute_mode: Literal["off", "vast"] = "off"
    # Every instance Krater rents is labelled `<prefix><job>-<attempt>`. The scheduler destroys any instance on the
    # account with this prefix that it has no record of, so give each Krater (staging, production) sharing a Vast
    # account its own prefix, and never start another tool's labels with it.
    compute_label_prefix: str = DEFAULT_COMPUTE_LABEL_PREFIX
    # Where a rented machine reaches Krater: the runner downloads itself from here and reports to it. Blank means
    # `base_url`; local staging sets its public tunnel URL.
    compute_runner_url: str = ""
    # The runner binary the portal serves and the worker checksums (built by the Dockerfile's runner stage).
    compute_runner_binary_path: str = "/app/runner/dist/krater-runner-linux-amd64"
    # The bucket for job checkpoints and outputs, on the same S3 storage as screenshots. Machines only ever get
    # presigned URLs for their own job's keys in it.
    compute_s3_bucket: str = "krater-compute"

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

    # Krater Compute's Vast.ai account (docs/compute/). Only the worker builds a Vast client; the org key never
    # reaches the portal or a member's machine. `fake` serves an in-memory Vast (`krater.vast.fake.FakeVast`) for
    # dev and tests, and nothing at all in production. `live` needs the key, preferably as a file
    # (`vast_api_key_file`, mounted into the worker alone); `vast_api_key` is for local runs.
    vast_mode: Literal["fake", "live"] = "fake"
    vast_api_url: str = "https://console.vast.ai"
    vast_api_key_file: str = ""
    vast_api_key: str = ""
    vast_timeout_seconds: float = 30.0
    # Project secrets (an HF_TOKEN a job needs) are stored encrypted with these Fernet keys
    # (`krater.services.project_secrets`): a file of one key per line, newest first, so a key can be rotated. Keep it
    # apart from the database and KRATER_SECRET_KEY. Without a key, project secrets are switched off.
    # `project_secrets_key` (comma-separated) is for local runs.
    project_secrets_key_file: str = ""
    project_secrets_key: str = ""
    # Krater's own Vast price history (Vast's history API is for host accounts only, and its public feed needs a
    # data license for bulk use). Every `price_history_interval_minutes`, one search per GPU model and rental type;
    # rows hold the min, p10 and median of GPU price plus `price_history_disk_gb` of disk, for 1-GPU offers. A run
    # costs about 500 of Vast's 20,000 daily search rows, so hourly (a divisor of 60, or whole hours); see
    # docs/compute/vast-api.md.
    price_history_interval_minutes: int = 60
    price_history_disk_gb: int = 32
    # Vast's own model names (checked against a live search on 2026-10-11).
    price_history_gpus: list[str] = [
        "RTX 3060",
        "RTX 3090",
        "RTX 4090",
        "RTX 5060 Ti",
        "RTX 5070 Ti",
        "RTX 5080",
        "RTX 5090",
        "RTX A4000",
        "RTX A6000",
        "RTX 6000Ada",
        "RTX PRO 6000 WS",
        "L4",
        "L40S",
        "A100 PCIE",
        "A100 SXM4",
        "H100 PCIE",
        "H100 SXM",
        "H200",
    ]

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

    @property
    def compute_on(self) -> bool:
        """Whether Krater Compute runs, with any provider."""
        return self.compute_mode != "off"

    @field_validator("compute_label_prefix")
    @classmethod
    def _label_prefix_is_specific(cls, value: str) -> str:
        # The scheduler destroys every instance with this prefix that it has no record of. An empty or bare
        # "krater" prefix would match other tools' machines on a shared Vast account.
        if len(value) < 7 or not value.endswith("-") or value.rstrip("-") == "krater":
            raise ValueError(
                "KRATER_COMPUTE_LABEL_PREFIX must be specific to this deployment and end with '-' (e.g. 'krater-prod-')"
            )
        return value

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Settings:
        if self.env == "production":
            if self.weave_mode == "stub":
                raise ValueError("KRATER_WEAVE_MODE cannot be 'stub' when KRATER_ENV=production")
            if self.pricing_mode == "fake":
                # Even with compute off: the budget estimator's GPU prices come from the public catalog.
                raise ValueError("KRATER_PRICING_MODE cannot be 'fake' when KRATER_ENV=production")
            if self.compute_mode == "vast" and self.vast_mode != "live":
                raise ValueError("KRATER_VAST_MODE must be 'live' when KRATER_COMPUTE_MODE=vast in production")
            if self.compute_on and self.compute_label_prefix == DEFAULT_COMPUTE_LABEL_PREFIX:
                # The scheduler destroys unknown instances with its prefix: two Kraters on one Vast account with the
                # same prefix would destroy each other's machines.
                raise ValueError("Set KRATER_COMPUTE_LABEL_PREFIX to this deployment's own prefix in production")
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
