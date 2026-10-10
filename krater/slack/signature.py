"""Verifying `X-Slack-Signature` on inbound requests to `/slack/interactions`.

See `docs/SPEC.md` "Slack integration" ("Slack signature verification...") and Slack's own docs: the
signature is an HMAC-SHA256 over `v0:{timestamp}:{raw request body}`, keyed by the app's signing secret,
compared in constant time. A stale timestamp is rejected to block replayed requests.
"""

from __future__ import annotations

import hashlib
import hmac
import time

#: Slack's own guidance: reject anything older than five minutes.
MAX_TIMESTAMP_AGE_SECONDS = 60 * 5


def verify_slack_signature(*, signing_secret: str, timestamp: str | None, body: bytes, signature: str | None) -> bool:
    """Whether `signature` (the `X-Slack-Signature` header) checks out for `body` and `timestamp` (the
    `X-Slack-Request-Timestamp` header), given the app's `signing_secret`.

    `False` for a missing/malformed timestamp or signature, a timestamp older than
    `MAX_TIMESTAMP_AGE_SECONDS`, or a mismatched HMAC -- callers don't need to distinguish why.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        timestamp_seconds = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - timestamp_seconds) > MAX_TIMESTAMP_AGE_SECONDS:
        return False

    base_string = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(signing_secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    expected_signature = f"v0={digest}"
    return hmac.compare_digest(expected_signature, signature)


__all__ = ["MAX_TIMESTAMP_AGE_SECONDS", "verify_slack_signature"]
