"""Exception types raised by `krater.vast`.

Callers branch on these the same way as `krater.slack.errors`: one for "couldn't complete the round trip"
(network error, timeout, a 5xx, a 429), one for "Vast understood the request and refused it". Messages carry
Vast's own `error`/`msg` fields, never a raw response body: a create response holds the new instance's own API
key, so bodies are not safe to log.
"""

from __future__ import annotations

from datetime import datetime

from krater.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderRequestFailedError,
    ProviderSearchQuotaError,
    ProviderUnavailableError,
)


class VastError(ProviderError):
    """Base class for every error raised by `krater.vast`."""


class VastUnavailableError(VastError, ProviderUnavailableError):
    """Vast couldn't be reached, timed out, answered 5xx, or rate-limited us (429). Safe to retry later."""


class VastRateLimitedError(VastUnavailableError, ProviderRateLimitedError):
    """Vast answered 429. Back off before the next call."""


class VastSearchQuotaError(VastRateLimitedError, ProviderSearchQuotaError):
    """Vast's daily quota of search rows is used up (429 `search_quota_exceeded`): no search works until
    `reset_at`. Other calls (listing, creating, destroying) still do."""

    def __init__(self, message: str, *, reset_at: datetime) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class VastRequestFailedError(VastError, ProviderRequestFailedError):
    """Vast answered and refused the request (a 4xx, or a 200 with `success: false`). `error` is Vast's
    machine-readable code (`invalid_args`, `no_such_instance`, ...) when it sent one."""

    def __init__(self, message: str, *, status_code: int | None = None, error: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error

    @property
    def may_have_happened(self) -> bool:
        # A 200 that says `success: false`: Vast has rented the machine anyway (staging, 2026-10-11).
        return self.status_code is not None and 200 <= self.status_code < 300


class VastRedirectError(VastRequestFailedError):
    """Vast answered with a redirect. On Vast's REST API that means a path without its trailing slash: the
    redirect is a 308 and **the request did nothing** (not even a DELETE). Always a bug in this package."""

    @property
    def may_have_happened(self) -> bool:
        return False
