"""A small in-process rate limiter for a handful of publicly-reachable POST-ish routes.

No Redis (see CLAUDE.md): buckets live in a plain module-level dict, so **limits are per worker process**
-- running N uvicorn workers gives each its own quota, not a shared one. Fine for the traffic this app
sees (a handful of members, one Slack workspace, SkyPilot polling), and matches "no Redis" for everything
else in this codebase.

Keyed by client IP for the routes that have no session to speak of (`/login`, `/auth/callback`, the
SkyPilot policy route, Slack's interactions endpoint), and by user id where one is signed in for POSTs in
general -- see `_bucket_key`. The client IP itself only comes from `X-Forwarded-For` when
`settings.trusted_proxy_count` says this app sits behind that many trusted reverse proxies; otherwise a
client could simply forge that header to spread requests across arbitrary "IPs" and dodge the limit
entirely, so we fall back to the socket peer address.

Disabled entirely under `KRATER_ENV=test` (see `RateLimitMiddleware.dispatch`) -- the test suite's
`login_as` fixture alone drives `/login` and `/auth/callback` dozens of times across the whole run, all
from the test client's one fake address, which would trip any limit generous enough to still mean
something in production. `tests/web/test_rate_limit.py` exercises the real logic directly instead, by
flipping a test's own settings to a non-test env and resetting `_buckets` between cases.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from krater.config import Settings
from krater.web.deps import SESSION_USER_ID_KEY

# --------------------------------------------------------------------------------------------------
# Token buckets
# --------------------------------------------------------------------------------------------------


@dataclass
class RateLimit:
    capacity: int
    per_seconds: float

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.per_seconds


class _Bucket:
    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: int) -> None:
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()


#: `(group, key) -> _Bucket`. Module-level and process-wide on purpose -- see the module docstring.
_buckets: dict[tuple[str, str], _Bucket] = {}

#: Once the table gets implausibly large (many distinct IPs/users), drop it rather than let it grow
#: forever. Simpler than per-entry expiry, and fine for this app's traffic.
_MAX_TRACKED_KEYS = 50_000


def reset_rate_limits() -> None:
    """Test hook: drop all bucket state, so tests don't leak quota into one another."""
    _buckets.clear()


def _take_token(group: str, key: str, limit: RateLimit, *, now: float | None = None) -> float | None:
    """Consume one token from `group`/`key`'s bucket. Returns `None` if allowed, else the number of
    seconds until a token will next be available (for `Retry-After`)."""
    if len(_buckets) > _MAX_TRACKED_KEYS:
        _buckets.clear()

    now = time.monotonic() if now is None else now
    bucket = _buckets.get((group, key))
    if bucket is None:
        bucket = _Bucket(limit.capacity)
        _buckets[(group, key)] = bucket

    elapsed = max(0.0, now - bucket.last_refill)
    bucket.tokens = min(limit.capacity, bucket.tokens + elapsed * limit.refill_per_second)
    bucket.last_refill = now

    if bucket.tokens >= 1:
        bucket.tokens -= 1
        return None

    seconds_needed = (1 - bucket.tokens) / limit.refill_per_second
    return seconds_needed


# --------------------------------------------------------------------------------------------------
# Client identification
# --------------------------------------------------------------------------------------------------


def get_client_ip(request: Request, settings: Settings) -> str:
    """The client's IP, honoring `X-Forwarded-For` only through `settings.trusted_proxy_count` hops.

    With N trusted proxies in front of this app, each is expected to append the address it received the
    request from to `X-Forwarded-For`, so the header looks like `client, proxy1, ..., proxyN-1` by the
    time it reaches us (the last proxy's own hop isn't in the header -- it's whoever connected to us).
    The real client is therefore the `N`th-from-the-right entry. A header shorter than that (fewer hops
    than configured -- a proxy skipped adding itself, or the header's missing) is untrustworthy, so we
    fall back to the socket peer address rather than reading a client-controlled entry as if it were
    trusted infrastructure.
    """
    if settings.trusted_proxy_count > 0:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
            index = len(hops) - settings.trusted_proxy_count
            if index >= 0:
                return hops[index]

    client = request.client
    return client.host if client is not None else "unknown"


def _signed_in_user_id(request: Request) -> str | None:
    try:
        session = request.session
    except AssertionError:
        # No SessionMiddleware installed (shouldn't happen in this app, but keeps this function safe to
        # call from anywhere). Falls back to IP-keying below.
        return None
    raw_user_id = session.get(SESSION_USER_ID_KEY)
    return str(raw_user_id) if raw_user_id else None


# --------------------------------------------------------------------------------------------------
# Route groups
# --------------------------------------------------------------------------------------------------

#: Generous on purpose (see CLAUDE.md-adjacent docs/skypilot-integration.md: SkyPilot calls this route
#: 2-3 times per `sky launch`/`validate`, from both its own server and every member's machine) -- this
#: exists to blunt a scanner or a runaway script hammering a publicly reachable, unauthenticated route,
#: not to throttle normal use.
_LOGIN_LIMIT = RateLimit(capacity=20, per_seconds=60)
_SKYPILOT_POLICY_LIMIT = RateLimit(capacity=120, per_seconds=60)
_SLACK_LIMIT = RateLimit(capacity=60, per_seconds=60)
_POST_GENERAL_LIMIT = RateLimit(capacity=120, per_seconds=60)

_AUTH_PATHS = frozenset({"/login", "/auth/callback"})
_SKYPILOT_POLICY_PATH = "/internal/skypilot/policy"
_SLACK_PATH = "/slack/interactions"


def _classify(request: Request) -> tuple[str, RateLimit, bool] | None:
    """`(group, limit, key_by_ip)` for `request`, or `None` if it isn't rate-limited at all."""
    path = request.url.path
    method = request.method

    if path in _AUTH_PATHS and method == "GET":
        return "auth", _LOGIN_LIMIT, True
    if path == _SKYPILOT_POLICY_PATH:
        return "skypilot_policy", _SKYPILOT_POLICY_LIMIT, True
    if path == _SLACK_PATH:
        return "slack", _SLACK_LIMIT, True
    if method == "POST":
        return "post_general", _POST_GENERAL_LIMIT, False
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """See the module docstring. Must sit *inside* `SessionMiddleware` (added to the app *before* it) so
    `request.session` is already populated when a route needs to key by signed-in user."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._settings.env == "test":
            return await call_next(request)

        classification = _classify(request)
        if classification is None:
            return await call_next(request)

        group, limit, key_by_ip = classification
        if key_by_ip:
            key = get_client_ip(request, self._settings)
        else:
            key = _signed_in_user_id(request) or get_client_ip(request, self._settings)

        retry_after = _take_token(group, key, limit)
        if retry_after is not None:
            return PlainTextResponse(
                "Too many requests. Please slow down.",
                status_code=429,
                headers={"Retry-After": str(math.ceil(retry_after))},
            )

        return await call_next(request)


__all__ = ["RateLimit", "RateLimitMiddleware", "get_client_ip", "reset_rate_limits"]
