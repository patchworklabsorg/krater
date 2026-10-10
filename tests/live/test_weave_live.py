"""Live Krater <-> Weave integration check: OIDC sign-in only.

Unlike the rest of the suite, this drives a **real** Weave (OIDC discovery/JWKS, the magic-link sign-in
flow and the `/oauth/authorize` consent screen) and a **real** running Krater in `KRATER_WEAVE_MODE=live`,
over plain HTTP -- no mocks. Weave owns Krater's roles: `scripts/dev/weave_e2e_provision.rb` gives the fixture
users Krater's app roles and marks the fixture `roles_provisioned`. The role checks skip without that mark.
See `docs/dev/weave-e2e.md` for how to bring both up and provision the fixture users this file reads.

It does the same OAuth Authorization Code + PKCE round trip a browser does (confirm a magic link, submit
the consent form, land back on Krater's `/auth/callback`), but drives it directly with `httpx` rather
than a browser: same redirects, same cookies, same real signed id_token, without a browser dependency in
the Python test suite. The interactive proof with an actual browser (Playwright/Chromium) is described in
that doc, alongside the exact bugs it caught that `httpx` alone could not (Turbo intercepting the sign-in
form, and `form-action` CSP blocking the OAuth redirect) -- both are Weave view/CSP issues invisible to a
plain HTTP client, which is why that manual pass still matters even with this file in place.

Every test here is marked `live` (deselected by default -- see `pyproject.toml`) and skips, individually
or at module scope, with a clear reason when what it needs isn't configured. Nothing here is required for
`uv run pytest`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import pytest

from krater.config import Settings
from krater.weave.live import LiveWeaveClient

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _env_path(name: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(name)
    if raw:
        return Path(raw)
    return default


FIXTURE_PATH = _env_path("WEAVE_E2E_FIXTURE", REPO_ROOT / ".weave_e2e_fixture.json")
KRATER_BASE_URL = os.environ.get("KRATER_LIVE_BASE_URL", "http://localhost:8201")
KRATER_DATABASE_URL = os.environ.get("KRATER_LIVE_DATABASE_URL") or os.environ.get("KRATER_DATABASE_URL")
WEAVE_REPO_DIR = _env_path("WEAVE_REPO_DIR", REPO_ROOT.parent / "weave")
RBENV_SHIMS_DIR = os.environ.get("RBENV_SHIMS_DIR", "/opt/rbenv/shims")


def _load_fixture() -> dict[str, Any] | None:
    if FIXTURE_PATH is None or not FIXTURE_PATH.exists():
        return None
    return json.loads(FIXTURE_PATH.read_text())


_FIXTURE = _load_fixture()

if _FIXTURE is None:
    pytest.skip(
        "no live-Weave fixture found (set WEAVE_E2E_FIXTURE, or run "
        "`uv run python scripts/dev/weave_e2e_setup.py` first -- see docs/dev/weave-e2e.md)",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    assert _FIXTURE is not None  # module-level skip above guarantees this
    return _FIXTURE


@pytest.fixture(scope="module")
def weave_settings(fixture: dict[str, Any]) -> Settings:
    return Settings(
        weave_mode="live",
        weave_issuer=fixture["issuer"],
        weave_client_id=fixture["oauth_client_id"],
        weave_client_secret=fixture["oauth_client_secret"],
    )


# --------------------------------------------------------------------------------------------------
# Krater's database: what sign-in recorded. Roles come from Weave; Krater only caches them.
# --------------------------------------------------------------------------------------------------


def _krater_dsn() -> str:
    if not KRATER_DATABASE_URL:
        pytest.skip("KRATER_LIVE_DATABASE_URL (or KRATER_DATABASE_URL) is not set")
    return KRATER_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


def _krater_user(email: str) -> dict[str, Any] | None:
    with psycopg.connect(_krater_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "select id, weave_sub, email_verified, last_login_at, roles_cached from users where email = %s", (email,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "weave_sub": row[1],
            "email_verified": row[2],
            "last_login_at": row[3],
            "roles": set(row[4]),
        }


def _needs_weave_roles(fixture: dict[str, Any]) -> None:
    """Skip unless the provisioning script gave the fixture users Krater app roles in Weave. An old fixture,
    written before the script created roles, has no `roles_provisioned` mark."""
    if not fixture.get("roles_provisioned"):
        pytest.skip("the Weave fixture has no Krater app roles; re-run scripts/dev/weave_e2e_setup.py")


@pytest.fixture(scope="module")
def weave_client(weave_settings: Settings) -> LiveWeaveClient:
    return LiveWeaveClient(weave_settings)


# --------------------------------------------------------------------------------------------------
# Weave's OIDC surface, via the real running Weave.
# --------------------------------------------------------------------------------------------------


def test_discovery_and_jwks_are_reachable(fixture: dict[str, Any]) -> None:
    resp = httpx.get(f"{fixture['issuer']}/.well-known/openid-configuration", timeout=10)
    resp.raise_for_status()
    doc = resp.json()
    assert doc["issuer"] == fixture["issuer"]
    assert "RS256" in doc["id_token_signing_alg_values_supported"]
    if "scopes_supported" in doc:
        assert {"openid", "profile", "email"} <= set(doc["scopes_supported"])

    jwks = httpx.get(doc["jwks_uri"], timeout=10)
    jwks.raise_for_status()
    assert jwks.json()["keys"]


def test_authorization_url_asks_for_the_roles_scopes(weave_settings: Settings, fixture: dict[str, Any]) -> None:
    client = LiveWeaveClient(weave_settings)

    url = client.authorization_url(
        state="s", nonce="n", code_verifier="v" * 64, redirect_uri=fixture.get("redirect_uri", "")
    )

    assert url.startswith(fixture["issuer"])
    assert parse_qs(urlparse(url).query)["scope"] == ["openid profile email groups roles slack"]


# --------------------------------------------------------------------------------------------------
# The full OAuth Authorization Code + PKCE round trip, against real running Krater + Weave.
# --------------------------------------------------------------------------------------------------


def _issue_magic_link(email: str) -> str:
    if WEAVE_REPO_DIR is None or not (WEAVE_REPO_DIR / "bin" / "rails").exists():
        pytest.skip(f"WEAVE_REPO_DIR ({WEAVE_REPO_DIR}) is not a Weave checkout -- can't mint a magic link")

    env = dict(os.environ)
    env["PATH"] = f"{RBENV_SHIMS_DIR}:{env.get('PATH', '')}"
    env.setdefault("DATABASE_URL", "postgres://root:root@localhost")
    env["RAILS_ENV"] = env.get("RAILS_ENV", "development")
    ruby = f'u = User.find_by!(email: {email!r}); puts User::MagicLink.issue!(u, requested_ip: "127.0.0.1").token'
    result = subprocess.run(
        ["bundle", "exec", "rails", "runner", ruby],
        cwd=WEAVE_REPO_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"could not mint a magic link for {email}:\n{result.stderr}")
    return result.stdout.strip().splitlines()[-1]


_AUTHENTICITY_TOKEN_RE = re.compile(r'name="authenticity_token"\s+value="([^"]*)"')
_HIDDEN_FIELD_RE = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')


def _authorize_form_fields(html: str) -> dict[str, str]:
    """The Doorkeeper consent screen's *first* form (POST -- "Authorize"; the second is the DELETE
    "Deny" form). Both share hidden field names, so this has to stop at the first `</form>`."""
    form_html = html.split("<form", 2)[1]
    form_html = form_html.split("</form>", 1)[0]
    fields = dict(_HIDDEN_FIELD_RE.findall(form_html))
    token_match = _AUTHENTICITY_TOKEN_RE.search(form_html)
    if token_match:
        fields["authenticity_token"] = token_match.group(1)
    fields["commit"] = "Authorize"
    return fields


@dataclass
class SignInResult:
    final_response: httpx.Response


def _sign_in(client: httpx.Client, fixture: dict[str, Any], email: str) -> SignInResult:
    """Drives the real Authorization Code + PKCE flow: Krater `/login` -> Weave's magic-link
    confirmation (using a freshly minted token in place of "the user clicked the emailed link") ->
    the OAuth consent screen (submitted for real, when Weave shows one) -> back to Krater's
    `/auth/callback`. Returns the final response Krater gave (a redirect on success, a 403 page for
    a non-member).
    """
    weave_base = fixture["issuer"]

    resp = client.get(f"{KRATER_BASE_URL}/login")
    assert resp.status_code == 302, f"Krater /login didn't redirect into Weave: {resp.status_code}"
    authorize_url = resp.headers["location"]
    assert authorize_url.startswith(weave_base), authorize_url

    # This GET is what puts client_id / the return-to authorize URL into Weave's own session
    # (AuthController#oauth_login) -- without it, confirming the magic link lands on Weave's
    # dashboard instead of resuming the OAuth flow.
    resp = client.get(authorize_url)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{weave_base}/oauth/login")

    token = _issue_magic_link(email)
    resp = client.get(f"{weave_base}/auth/magic_link/{token}")
    assert resp.status_code == 200, "magic link wasn't live (already used, expired, or unknown)"
    csrf = _AUTHENTICITY_TOKEN_RE.search(resp.text)
    assert csrf, "no CSRF token on the magic-link confirmation page"

    resp = client.post(
        f"{weave_base}/auth/magic_link/{token}",
        data={"authenticity_token": csrf.group(1)},
    )
    assert resp.status_code == 302, f"magic-link confirmation didn't redirect: {resp.status_code}"
    next_url = resp.headers["location"]

    if next_url.startswith(f"{weave_base}/oauth/authorize"):
        resp = client.get(next_url)
        if resp.status_code == 200:
            # First-time consent: submit the real "Authorize" form.
            fields = _authorize_form_fields(resp.text)
            resp = client.post(f"{weave_base}/oauth/authorize", data=fields)
        assert resp.status_code == 302, f"OAuth authorize didn't redirect: {resp.status_code} {resp.text[:300]}"
        next_url = resp.headers["location"]

    assert next_url.startswith(f"{KRATER_BASE_URL}/auth/callback"), next_url
    final = client.get(next_url)
    return SignInResult(final_response=final)


@pytest.fixture
def http_client() -> Iterator[httpx.Client]:
    with httpx.Client(follow_redirects=False, timeout=15) as client:
        yield client


def test_signin_persists_a_member_and_their_weave_roles(fixture: dict[str, Any], http_client: httpx.Client) -> None:
    _needs_weave_roles(fixture)
    email = fixture["users"]["member"]["email"]

    result = _sign_in(http_client, fixture, email)

    assert result.final_response.status_code == 302, "sign-in should end with Krater redirecting home"
    row = _krater_user(email)
    assert row is not None, "Krater never created/updated the user row"
    assert row["weave_sub"] == fixture["users"]["member"]["sub"]
    assert row["email_verified"] is True, "Weave's id_token should carry email_verified=true"
    assert row["roles"] == {"ganymede:member"}
    assert row["last_login_at"] is not None


def test_signin_maps_the_admin_role(fixture: dict[str, Any], http_client: httpx.Client) -> None:
    _needs_weave_roles(fixture)
    admin = fixture["users"]["admin"]

    result = _sign_in(http_client, fixture, admin["email"])

    assert result.final_response.status_code == 302
    row = _krater_user(admin["email"])
    assert row is not None
    assert {"ganymede:member", "ganymede:admin"} <= row["roles"]


def test_signin_refuses_a_non_member(fixture: dict[str, Any], http_client: httpx.Client) -> None:
    email = fixture["users"]["non_member"]["email"]

    result = _sign_in(http_client, fixture, email)

    assert result.final_response.status_code == 403
    assert "Ask a Ganymede admin" in result.final_response.text
    row = _krater_user(email)
    assert row is not None
    assert "ganymede:member" not in row["roles"]


def test_directory_get_user_matches_the_fixture(fixture: dict[str, Any], weave_client: LiveWeaveClient) -> None:
    _needs_weave_roles(fixture)
    member = fixture["users"]["member"]

    record = weave_client.get_user(member["sub"])

    assert record is not None
    assert record.active is True
    assert "ganymede:member" in record.roles


def test_directory_unknown_sub_returns_none(fixture: dict[str, Any], weave_client: LiveWeaveClient) -> None:
    _needs_weave_roles(fixture)

    assert weave_client.get_user("PWLDOESNOTEXIST") is None


def test_directory_lists_admins(fixture: dict[str, Any], weave_client: LiveWeaveClient) -> None:
    _needs_weave_roles(fixture)

    subs = {record.sub for record in weave_client.list_users_with_role("ganymede:admin")}

    assert fixture["users"]["admin"]["sub"] in subs
