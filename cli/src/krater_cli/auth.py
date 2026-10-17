"""Signing in to Krater with Weave: OAuth 2.0 authorization code with PKCE, for the Krater CLI's public Weave app.

Krater says where Weave is and which app to use (`GET /api/v1/cli-config`). By default Weave shows the code after
sign-in and the member pastes it here (Doorkeeper's out-of-band redirect), which works over SSH too. When Krater
says the app allows it (`loopback`), the CLI listens on 127.0.0.1 for the redirect instead. Access tokens last a
couple of hours; the CLI refreshes them with the refresh token as needed.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from krater_cli import store

#: Refresh this long before the access token expires.
REFRESH_MARGIN_SECONDS = 60
LOOPBACK_WAIT_SECONDS = 300


class AuthError(Exception):
    """Signing in failed, or the stored sign-in can't be used any more."""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _json(response: httpx.Response, what: str) -> dict:
    if response.status_code != 200:
        raise AuthError(f"{what} failed ({response.status_code}).")
    try:
        data = response.json()
    except ValueError:
        raise AuthError(f"{what} didn't answer in JSON.") from None
    if not isinstance(data, dict):
        raise AuthError(f"{what} gave an unexpected answer.")
    return data


class _LoopbackServer:
    """Receives Weave's redirect on 127.0.0.1:<free port>/callback, once."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = parse_qs(urlparse(self.path).query)
                outer.code = (query.get("code") or [None])[0]
                outer.state = (query.get("state") or [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Signed in to Krater. You can close this tab.")

            def log_message(self, *args) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_port}/callback"

    def wait(self, timeout: float) -> tuple[str | None, str | None]:
        thread = threading.Thread(target=self._server.handle_request, daemon=True)
        self._server.timeout = timeout
        thread.start()
        thread.join(timeout + 1)
        self._server.server_close()
        return self.code, self.state


def login(
    url: str,
    *,
    http: httpx.Client,
    open_browser: bool = True,
    prompt: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> dict:
    """Sign in and store the tokens. Returns what was stored."""
    url = url.rstrip("/")
    config = _json(http.get(f"{url}/api/v1/cli-config"), "Asking Krater how to sign in")
    discovery = _json(
        http.get(f"{config['issuer'].rstrip('/')}/.well-known/openid-configuration"), "Reading Weave's configuration"
    )
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    loopback = _LoopbackServer() if config.get("loopback") and open_browser else None
    redirect_uri = loopback.redirect_uri if loopback is not None else config["redirect_uri"]
    authorize = (
        discovery["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": config["client_id"],
                "redirect_uri": redirect_uri,
                "scope": config.get("scopes", "openid profile"),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    say(f"Sign in with Weave:\n\n  {authorize}\n")
    if open_browser:
        try:
            webbrowser.open(authorize)
        except webbrowser.Error:
            pass
    if loopback is not None:
        code, returned_state = loopback.wait(LOOPBACK_WAIT_SECONDS)
        if code is None:
            raise AuthError("No sign-in arrived. Try `krater login --no-browser`.")
        if returned_state != state:
            raise AuthError("The sign-in didn't match this request; try again.")
    else:
        code = prompt("Paste the code Weave shows after you sign in: ").strip()
        if not code:
            raise AuthError("No code given.")

    token = _json(
        http.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config["client_id"],
                "code_verifier": verifier,
            },
        ),
        "Exchanging the code",
    )
    creds = {
        "url": url,
        "client_id": config["client_id"],
        "token_endpoint": discovery["token_endpoint"],
        **_token_fields(token),
    }
    store.save(creds)
    return creds


def _token_fields(token: dict) -> dict:
    if not isinstance(token.get("access_token"), str):
        raise AuthError("Weave's answer had no access token.")
    expires_in = token.get("expires_in")
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": time.time() + (expires_in if isinstance(expires_in, int | float) else 3600),
    }


def _refresh_failure(status: int) -> AuthError:
    if status == 429 or status >= 500:
        return AuthError("Weave is unavailable right now. Try again in a moment.")
    if status in (400, 401):
        return AuthError("Your sign-in has expired. Run `krater login`.")
    return AuthError(f"Weave refused to refresh your sign-in ({status}). Run `krater login` if this keeps happening.")


def refresh(creds: dict, *, http: httpx.Client) -> dict:
    """New tokens from the refresh token, stored. Raises `AuthError` if Weave won't (sign in again).

    Weave rotates the refresh token on every use. If another `krater` process refreshed since this one loaded its
    sign-in, our refresh token is spent but the stored one is good: when a refusal comes and the file holds a different
    refresh token, try that one (once) instead of telling the member to sign in again.
    """
    if not creds.get("refresh_token"):
        raise AuthError("Your sign-in has expired. Run `krater login`.")
    retried = False
    while True:
        response = http.post(
            creds["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": creds["client_id"],
            },
        )
        if response.status_code == 200:
            break
        stored = store.load() or {}
        newer = stored.get("refresh_token")
        if not retried and newer and newer != creds["refresh_token"] and response.status_code in (400, 401):
            retried = True
            creds = {**creds, **stored}
            continue
        raise _refresh_failure(response.status_code)
    updated = {**creds, **_token_fields(_json(response, "Refreshing your sign-in"))}
    if not updated.get("refresh_token"):
        updated["refresh_token"] = creds["refresh_token"]
    store.save(updated)
    return updated


def revoke(creds: dict, *, http: httpx.Client) -> bool:
    """Ask Weave to invalidate the stored tokens (Doorkeeper's `/oauth/revoke`, next to the token endpoint).

    Best effort: returns whether Weave accepted both. Doorkeeper answers 200 for a token it doesn't know.
    """
    endpoint = str(creds.get("token_endpoint") or "")
    if not endpoint or not creds.get("client_id"):
        return False
    url = endpoint.rsplit("/", 1)[0] + "/revoke"
    ok = True
    for hint, key in (("refresh_token", "refresh_token"), ("access_token", "access_token")):
        if not creds.get(key):
            continue
        try:
            response = http.post(
                url, data={"token": creds[key], "token_type_hint": hint, "client_id": creds["client_id"]}
            )
        except httpx.HTTPError:
            ok = False
            continue
        ok = ok and response.status_code == 200
    return ok


def current(*, http: httpx.Client) -> dict:
    """The stored sign-in, refreshed if its access token is about to expire."""
    creds = store.load()
    if creds is None:
        raise AuthError("Not signed in. Run `krater login --url https://<your Krater>`.")
    if creds.get("expires_at", 0) - REFRESH_MARGIN_SECONDS < time.time():
        creds = refresh(creds, http=http)
    return creds
