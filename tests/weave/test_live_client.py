"""`LiveWeaveClient` against a fake Weave (`httpx.MockTransport`): discovery, JWKS, PKCE, id_token
validation (signature, `iss`, `aud`, `exp`, `nonce`), the roles and Slack claims, and the directory API
with its client_credentials token.
"""

from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from krater.config import Settings
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave import live as live_module
from krater.weave.errors import WeaveAuthError, WeaveUnavailableError
from krater.weave.live import LiveWeaveClient
from krater.weave.types import WeaveUser

ISSUER = "https://weave.test"
CLIENT_ID = "krater-client"
CLIENT_SECRET = "krater-secret"
KID = "test-key-1"
REDIRECT_URI = "https://krater.test/auth/callback"
API_BASE = "https://api.weave.test"
GOOD_NONCE = "expected-nonce"


def _settings() -> Settings:
    return Settings(
        weave_issuer=ISSUER,
        weave_client_id=CLIENT_ID,
        weave_client_secret=CLIENT_SECRET,
        weave_api_base_url=API_BASE,
    )


def _id_token(key: RSAKey, **claim_overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "PWLLIVE0001",
        "name": "Lee Live",
        "email": "lee@example.com",
        "email_verified": True,
        "roles": ["member"],
        "nonce": GOOD_NONCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(claim_overrides)
    return jwt.encode({"alg": "RS256", "kid": KID}, claims, key)


class FakeWeave:
    """A minimal fake of Weave's OIDC and directory endpoints, driven by an `httpx.MockTransport`."""

    def __init__(self, signing_key: RSAKey) -> None:
        self.signing_key = signing_key
        self.requests: list[httpx.Request] = []
        self.id_token: str | None = None
        self.token_status = 200
        self.directory_users: dict[str, dict] = {}
        self.issued_tokens: list[str] = []
        self.valid_tokens: set[str] = set()
        self.expires_in: object = 3600
        self.directory_status: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "jwks_uri": f"{ISSUER}/oauth/discovery/keys",
                },
            )
        if path == "/oauth/discovery/keys":
            return httpx.Response(200, json={"keys": [self.signing_key.as_dict(private=False)]})
        if path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["client_credentials"]:
                token = f"cc-{len(self.issued_tokens) + 1}"
                self.issued_tokens.append(token)
                self.valid_tokens.add(token)
                return httpx.Response(
                    200, json={"access_token": token, "token_type": "Bearer", "expires_in": self.expires_in}
                )
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": self.id_token})
        if path.startswith("/api/v1/directory/"):
            assert request.url.host == "api.weave.test"
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            if bearer not in self.valid_tokens:
                return httpx.Response(401, json={"error": "invalid_token"})
            if self.directory_status is not None:
                return httpx.Response(self.directory_status, json={"error": "boom"})
            if path == "/api/v1/directory/users":
                role = request.url.params.get("role")
                if role == "unlinked":
                    return httpx.Response(404, json={"error": "not_found"})
                users = sorted(
                    (u for u in self.directory_users.values() if role in u.get("roles", [])), key=lambda u: u["sub"]
                )
                return httpx.Response(200, json={"users": users})
            sub = path.rsplit("/", 1)[-1]
            user = self.directory_users.get(sub)
            return httpx.Response(404, json={"error": "not_found"}) if user is None else httpx.Response(200, json=user)
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture(scope="module")
def rsa_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)


@pytest.fixture
def fake_weave(rsa_key: RSAKey) -> FakeWeave:
    return FakeWeave(rsa_key)


@pytest.fixture
def live_client(fake_weave: FakeWeave) -> LiveWeaveClient:
    http_client = httpx.Client(transport=httpx.MockTransport(fake_weave.handler))
    return LiveWeaveClient(_settings(), http_client=http_client)


def test_authorization_url_uses_s256_pkce_and_the_roles_scopes(live_client: LiveWeaveClient) -> None:
    url = live_client.authorization_url(state="s1", nonce="n1", code_verifier="a" * 64, redirect_uri=REDIRECT_URI)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert url.startswith(f"{ISSUER}/oauth/authorize?")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == ["openid profile email groups roles slack"]
    assert params["state"] == ["s1"]
    assert params["nonce"] == ["n1"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]  # a non-empty S256 challenge was derived from the verifier


def test_exchange_code_accepts_a_good_token(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(rsa_key)

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    assert identity.sub == "PWLLIVE0001"
    assert identity.name == "Lee Live"
    assert identity.email == "lee@example.com"
    assert identity.email_verified is True


def test_exchange_code_maps_the_roles_claim_and_reads_the_slack_claims(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(
        rsa_key,
        roles=["member", "reviewer", "unknown"],
        groups=["krater-admins"],
        slack_id="U1234",
        slack_member=True,
    )

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    # The roles claim decides: the admin group slug is ignored because `roles` is present.
    assert identity.roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    assert identity.slack_id == "U1234"
    assert identity.slack_member is True


def test_exchange_code_falls_back_to_groups_without_a_roles_claim(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    # Signed without a `roles` claim at all.
    now = int(time.time())
    fake_weave.id_token = jwt.encode(
        {"alg": "RS256", "kid": KID},
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "PWLLIVE0001",
            "groups": ["ganymede-members", "krater-admins"],
            "nonce": GOOD_NONCE,
            "iat": now,
            "exp": now + 300,
        },
        rsa_key,
    )

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    assert identity.roles == frozenset({GROUP_MEMBER, GROUP_ADMIN})
    assert identity.slack_member is None


@pytest.mark.parametrize("email_verified", [False, "true", None], ids=["false", "string", "absent"])
def test_exchange_code_only_trusts_a_boolean_true_email_verified(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey, email_verified: object
) -> None:
    fake_weave.id_token = _id_token(rsa_key, email_verified=email_verified)

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    assert identity.email_verified is False


def test_exchange_code_sends_the_client_secret_and_code_verifier(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(rsa_key)

    live_client.exchange_code(code="c1", code_verifier="verifier-value", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    token_request = next(r for r in fake_weave.requests if r.url.path == "/oauth/token")
    body = token_request.read().decode()
    assert "code_verifier=verifier-value" in body
    assert f"client_secret={CLIENT_SECRET}" in body


@pytest.mark.parametrize(
    "override",
    [
        {"aud": "someone-elses-client"},
        {"iss": "https://not-weave.test"},
        {"nonce": "wrong-nonce"},
        {"exp": int(time.time()) - 60, "iat": int(time.time()) - 120},
    ],
    ids=["wrong-aud", "wrong-iss", "wrong-nonce", "expired"],
)
def test_exchange_code_rejects_bad_claims(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey, override: dict
) -> None:
    fake_weave.id_token = _id_token(rsa_key, **override)

    with pytest.raises(WeaveAuthError):
        live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)


def test_exchange_code_rejects_a_bad_signature(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    # Signed by a different key than the one Weave's JWKS publishes, but with the same `kid` -- a real
    # forgery attempt would look exactly like this.
    forged_key = RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)
    fake_weave.id_token = _id_token(forged_key)

    with pytest.raises(WeaveAuthError):
        live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)


def test_exchange_code_raises_weave_auth_error_when_the_code_is_rejected(
    fake_weave: FakeWeave, live_client: LiveWeaveClient
) -> None:
    fake_weave.token_status = 400

    with pytest.raises(WeaveAuthError):
        live_client.exchange_code(code="bad-code", code_verifier="v", redirect_uri=REDIRECT_URI, nonce="n")


def test_exchange_code_raises_weave_unavailable_on_a_5xx(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.token_status = 500

    with pytest.raises(WeaveUnavailableError):
        live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce="n")


def test_discovery_and_jwks_are_each_fetched_only_once(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(rsa_key)

    live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)
    live_client.authorization_url(state="s", nonce="n", code_verifier="v", redirect_uri=REDIRECT_URI)

    discovery_hits = [r for r in fake_weave.requests if r.url.path == "/.well-known/openid-configuration"]
    jwks_hits = [r for r in fake_weave.requests if r.url.path == "/oauth/discovery/keys"]
    assert len(discovery_hits) == 1
    assert len(jwks_hits) == 1


# -- Directory API -----------------------------------------------------------------------------------

DIRECTORY_USER = {
    "sub": "PWLDIR0001",
    "name": "Dee Rectory",
    "email": "dee@example.com",
    "email_verified": True,
    "slack_id": "U_DEE",
    "slack_member": False,
    "groups": ["krater-admins"],
    "roles": ["member", "reviewer"],
    "active": True,
}


def _token_requests(fake_weave: FakeWeave) -> list[httpx.Request]:
    return [r for r in fake_weave.requests if r.url.path == "/oauth/token"]


def test_get_user_parses_a_directory_record(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0001"] = DIRECTORY_USER

    user = live_client.get_user("PWLDIR0001")

    assert user == WeaveUser(
        sub="PWLDIR0001",
        name="Dee Rectory",
        email="dee@example.com",
        email_verified=True,
        slack_id="U_DEE",
        slack_member=False,
        roles=frozenset({GROUP_MEMBER, GROUP_REVIEWER}),
        active=True,
    )


def test_get_user_answers_none_on_404(live_client: LiveWeaveClient) -> None:
    assert live_client.get_user("PWLNOBODY") is None


def test_a_record_without_active_true_is_inactive(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    record = {key: value for key, value in DIRECTORY_USER.items() if key != "active"}
    fake_weave.directory_users["PWLDIR0001"] = record

    user = live_client.get_user("PWLDIR0001")

    assert user is not None and user.active is False


def test_the_client_credentials_token_is_requested_with_the_directory_scope_and_cached(
    fake_weave: FakeWeave, live_client: LiveWeaveClient
) -> None:
    fake_weave.directory_users["PWLDIR0001"] = DIRECTORY_USER

    live_client.get_user("PWLDIR0001")
    live_client.get_user("PWLNOBODY")
    live_client.list_users_with_role(GROUP_REVIEWER)

    token_requests = _token_requests(fake_weave)
    assert len(token_requests) == 1
    form = parse_qs(token_requests[0].content.decode())
    assert form == {"grant_type": ["client_credentials"], "scope": ["directory"]}
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert token_requests[0].headers["authorization"] == f"Basic {expected_basic}"
    directory_requests = [r for r in fake_weave.requests if r.url.path.startswith("/api/v1/directory/")]
    assert {r.headers["authorization"] for r in directory_requests} == {"Bearer cc-1"}


def test_the_token_is_refetched_shortly_before_it_expires(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    # 20 seconds is inside the refresh margin, so the cached token is never reused.
    fake_weave.expires_in = 20

    live_client.get_user("PWLA")
    live_client.get_user("PWLB")

    assert len(_token_requests(fake_weave)) == 2


def test_a_401_refetches_the_token_once_and_retries(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0001"] = DIRECTORY_USER
    live_client.get_user("PWLNOBODY")
    fake_weave.valid_tokens.clear()  # Weave revoked the cached token

    user = live_client.get_user("PWLDIR0001")

    assert user is not None
    assert fake_weave.issued_tokens == ["cc-1", "cc-2"]


def test_a_second_401_fails_closed(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_status = 401

    with pytest.raises(WeaveUnavailableError):
        live_client.get_user("PWLDIR0001")
    assert len(_token_requests(fake_weave)) == 2


def test_a_refused_token_request_fails_closed(rsa_key: RSAKey) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    client = LiveWeaveClient(_settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(WeaveUnavailableError):
        client.get_user("PWLDIR0001")


def test_a_directory_5xx_fails_closed(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_status = 500

    with pytest.raises(WeaveUnavailableError):
        live_client.get_user("PWLDIR0001")


def test_an_unreachable_weave_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = LiveWeaveClient(_settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(WeaveUnavailableError):
        client.get_user("PWLDIR0001")


def test_get_user_answers_are_cached_briefly(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0001"] = DIRECTORY_USER

    live_client.get_user("PWLDIR0001")
    live_client.get_user("PWLDIR0001")

    lookups = [r for r in fake_weave.requests if r.url.path == "/api/v1/directory/users/PWLDIR0001"]
    assert len(lookups) == 1


def test_a_fresh_lookup_skips_the_cache_and_refreshes_it(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0001"] = DIRECTORY_USER
    live_client.get_user("PWLDIR0001")

    # Weave drops the reviewer role inside the cache window: a fresh lookup must see it straight away...
    fake_weave.directory_users["PWLDIR0001"] = {**DIRECTORY_USER, "roles": ["member"]}
    fresh = live_client.get_user("PWLDIR0001", fresh=True)
    # ...and the cache now holds the newer answer for page views.
    cached = live_client.get_user("PWLDIR0001")

    assert fresh is not None and fresh.roles == frozenset({GROUP_MEMBER})
    assert cached == fresh
    lookups = [r for r in fake_weave.requests if r.url.path == "/api/v1/directory/users/PWLDIR0001"]
    assert len(lookups) == 2


def test_list_users_with_role_sends_the_weave_role_key(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0001"] = DIRECTORY_USER
    fake_weave.directory_users["PWLDIR0002"] = {**DIRECTORY_USER, "sub": "PWLDIR0002", "roles": ["member"]}

    reviewers = live_client.list_users_with_role(GROUP_REVIEWER)

    assert [u.sub for u in reviewers] == ["PWLDIR0001"]
    list_request = next(r for r in fake_weave.requests if r.url.path == "/api/v1/directory/users")
    assert list_request.url.params["role"] == "reviewer"


def test_a_403_fails_closed_without_refetching_the_token(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_status = 403

    with pytest.raises(WeaveUnavailableError, match="403"):
        live_client.get_user("PWLDIR0001")
    assert len(_token_requests(fake_weave)) == 1


def test_a_role_weave_does_not_link_to_krater_is_an_empty_list(
    fake_weave: FakeWeave, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(live_module.logger, "warning", lambda msg, *args: warnings.append(msg % args))
    settings = _settings().model_copy(update={"weave_role_admin": "unlinked"})
    client = LiveWeaveClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(fake_weave.handler)))

    assert client.list_users_with_role(GROUP_ADMIN) == []
    assert any("unlinked" in warning for warning in warnings)


def test_a_bare_list_response_is_rejected_as_malformed(live_client: LiveWeaveClient, fake_weave: FakeWeave) -> None:
    original = fake_weave.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/api/v1/directory/users":
            return httpx.Response(200, json=response.json()["users"])
        return response

    client = LiveWeaveClient(_settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(WeaveUnavailableError):
        client.list_users_with_role(GROUP_REVIEWER)
