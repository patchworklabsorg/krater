"""Exception types raised by `krater.weave`.

Callers (routers, deps) branch on these two, not on HTTP status codes or Weave's own exceptions.
"""

from __future__ import annotations


class WeaveError(Exception):
    """Base class for every error raised by `krater.weave`."""


class WeaveAuthError(WeaveError):
    """Sign-in failed: a bad state/PKCE pairing, a code that didn't exchange, or an id_token that didn't
    verify (signature, issuer, audience, expiry or nonce)."""


class WeaveUnavailableError(WeaveError):
    """Weave couldn't be reached, or answered with an error we can't attribute to the caller (a 5xx, a
    malformed response, a network timeout)."""
