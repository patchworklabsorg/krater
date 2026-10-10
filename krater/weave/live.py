"""`LiveWeaveClient`: OIDC sign-in and the directory API against a real Weave.

Discovery and the JWKS are fetched once per process and cached. id_tokens are RS256, verified against
that JWKS with `joserfc` (signature, `iss`, `aud`, `exp`, `nonce`); PKCE is S256, as Weave requires.

The directory API is called with an access token from Krater's own OAuth app (the client_credentials
grant, scope `directory`). The token is cached until shortly before it expires, and refetched once if
Weave answers 401. `get_user` answers are cached for a short TTL to absorb bursts of lookups from page
views; `get_user(fresh=True)`, which every state-changing action uses, skips that cache. See
`docs/weave-integration.md` for the full contract.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from urllib.parse import quote, urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from krater.config import Settings
from krater.weave.cache import MISSING, TTLCache
from krater.weave.errors import WeaveAuthError, WeaveUnavailableError
from krater.weave.roles import RoleMapping
from krater.weave.types import WeaveIdentity, WeaveUser

logger = logging.getLogger(__name__)

SCOPES = "openid profile email groups roles slack"

#: The scope Krater asks for on its client_credentials token for the directory API.
DIRECTORY_SCOPE = "directory"

#: How long a `get_user` answer is trusted before re-fetching. Page views only: `fresh=True` lookups ignore it.
DIRECTORY_CACHE_TTL_SECONDS = 60.0

#: Refetch the client_credentials token this long before Weave says it expires.
TOKEN_EXPIRY_MARGIN_SECONDS = 30.0

#: Used when the token response has no usable `expires_in`.
DEFAULT_TOKEN_LIFETIME_SECONDS = 300.0


def _pkce_code_challenge_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class LiveWeaveClient:
    """A `WeaveClient` backed by a real Weave over HTTP. `http_client` is injectable for tests
    (`httpx.MockTransport`); production code leaves it out and gets a real `httpx.Client`."""

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http = http_client if http_client is not None else httpx.Client(timeout=10.0)
        self._roles = RoleMapping.from_settings(settings)
        self._discovery_doc: dict | None = None
        self._jwk_set: KeySet | None = None
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._directory_cache: TTLCache[str, WeaveUser | None] = TTLCache(DIRECTORY_CACHE_TTL_SECONDS)

    # -- OIDC ------------------------------------------------------------------------------------

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        endpoint = self._discovery()["authorization_endpoint"]
        params = {
            "response_type": "code",
            "client_id": self._settings.weave_client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_code_challenge_s256(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{endpoint}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        token_endpoint = self._discovery()["token_endpoint"]
        try:
            response = self._http.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self._settings.weave_client_id,
                    "client_secret": self._settings.weave_client_secret,
                    "code_verifier": code_verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError("could not reach Weave's token endpoint") from exc

        if response.status_code >= 500:
            raise WeaveUnavailableError(f"Weave's token endpoint returned {response.status_code}")
        if response.status_code != 200:
            raise WeaveAuthError(f"token exchange failed: {response.status_code} {response.text}")

        id_token = response.json().get("id_token")
        if not id_token:
            raise WeaveAuthError("token response had no id_token")

        return self._verify_id_token(id_token, nonce=nonce)

    def _discovery(self) -> dict:
        if self._discovery_doc is None:
            self._discovery_doc = self._get_json(f"{self._settings.weave_issuer}/.well-known/openid-configuration")
        return self._discovery_doc

    def _jwks(self) -> KeySet:
        if self._jwk_set is None:
            jwks_uri = self._discovery()["jwks_uri"]
            self._jwk_set = KeySet.import_key_set(self._get_json(jwks_uri))
        return self._jwk_set

    def _get_json(self, url: str) -> dict:
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError(f"could not reach Weave at {url}") from exc
        if response.status_code != 200:
            raise WeaveUnavailableError(f"Weave returned {response.status_code} for {url}")
        return response.json()

    def _verify_id_token(self, id_token: str, *, nonce: str) -> WeaveIdentity:
        try:
            token = jwt.decode(id_token, self._jwks(), algorithms=["RS256"])
        except JoseError as exc:
            raise WeaveAuthError(f"id_token failed signature/decoding checks: {exc}") from exc

        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": self._settings.weave_issuer},
            aud={"essential": True, "value": self._settings.weave_client_id},
            exp={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        try:
            registry.validate(token.claims)
        except JoseError as exc:
            raise WeaveAuthError(f"id_token claims failed validation: {exc}") from exc

        claims = token.claims
        return WeaveIdentity(
            sub=claims["sub"],
            name=_string(claims.get("name")),
            email=_string(claims.get("email")),
            email_verified=claims.get("email_verified") is True,
            slack_id=_string(claims.get("slack_id")) or None,
            slack_member=_optional_bool(claims.get("slack_member")),
            roles=self._roles.krater_roles(roles=claims.get("roles"), groups=claims.get("groups")),
        )

    # -- Directory -------------------------------------------------------------------------------

    def get_user(self, sub: str, *, fresh: bool = False) -> WeaveUser | None:
        if not fresh:
            cached = self._directory_cache.get(sub)
            if cached is not MISSING:
                return cached

        response = self._directory_get(f"/api/v1/directory/users/{quote(sub, safe='')}")
        if response.status_code == 404:
            user: WeaveUser | None = None
        else:
            user = self._parse_user(self._directory_json(response))

        self._directory_cache.set(sub, user)
        return user

    def list_users_with_role(self, role: str) -> list[WeaveUser]:
        key = self._roles.weave_role_key(role)
        response = self._directory_get("/api/v1/directory/users", params={"role": key})
        if response.status_code == 404:
            # Weave answers 404 for a role that isn't defined on (or linked to) the Krater app.
            logger.warning("Weave's directory doesn't know Krater role key %r; treating it as nobody", key)
            return []
        data = self._directory_json(response)
        entries = data.get("users") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise WeaveUnavailableError("Weave's directory returned a malformed user list")
        return [self._parse_user(entry) for entry in entries]

    def _directory_get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        base = (self._settings.weave_api_base_url or self._settings.weave_issuer).rstrip("/")
        url = f"{base}{path}"
        response = self._authorized_get(url, params)
        if response.status_code == 401:
            # The token may have been revoked or rotated early: fetch a new one and try once more.
            self._access_token = None
            response = self._authorized_get(url, params)
        if response.status_code == 403:
            # Not a token problem, so no refetch: the token lacks the `directory` scope, or Weave doesn't
            # let this app use the directory. Only an admin fixing Weave's config helps.
            logger.error(
                "Weave's directory refused Krater (403): add the `directory` scope to the Krater app in "
                "Weave and check KRATER_WEAVE_CLIENT_ID/SECRET"
            )
            raise WeaveUnavailableError("Weave's directory refused Krater's token (403); check the app config")
        if response.status_code not in (200, 404):
            raise WeaveUnavailableError(f"Weave's directory returned {response.status_code} for {path}")
        return response

    def _authorized_get(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._directory_token()}"}
        try:
            return self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError(f"could not reach Weave's directory at {url}") from exc

    def _directory_token(self) -> str:
        """Krater's client_credentials access token, cached until shortly before it expires."""
        if self._access_token is not None and time.monotonic() < self._access_token_expires_at:
            return self._access_token

        token_url = f"{self._settings.weave_issuer.rstrip('/')}/oauth/token"
        try:
            response = self._http.post(
                token_url,
                data={"grant_type": "client_credentials", "scope": DIRECTORY_SCOPE},
                auth=(self._settings.weave_client_id, self._settings.weave_client_secret),
            )
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError("could not reach Weave's token endpoint") from exc
        if response.status_code != 200:
            raise WeaveUnavailableError(f"Weave refused Krater's directory token: {response.status_code}")

        body = response.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise WeaveUnavailableError("Weave's token response had no access_token")
        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else DEFAULT_TOKEN_LIFETIME_SECONDS

        self._access_token = token
        self._access_token_expires_at = time.monotonic() + max(lifetime - TOKEN_EXPIRY_MARGIN_SECONDS, 0.0)
        return token

    @staticmethod
    def _directory_json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise WeaveUnavailableError("Weave's directory returned malformed JSON") from exc

    def _parse_user(self, data: object) -> WeaveUser:
        if not isinstance(data, dict) or not isinstance(data.get("sub"), str):
            raise WeaveUnavailableError("Weave's directory returned a malformed user record")
        return WeaveUser(
            sub=data["sub"],
            name=_string(data.get("name")),
            email=_string(data.get("email")),
            email_verified=data.get("email_verified") is True,
            slack_id=_string(data.get("slack_id")) or None,
            slack_member=_optional_bool(data.get("slack_member")),
            roles=self._roles.krater_roles(roles=data.get("roles"), groups=data.get("groups")),
            # Fail closed: only an explicit `true` counts as active.
            active=data.get("active") is True,
        )


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


__all__ = ["LiveWeaveClient"]
