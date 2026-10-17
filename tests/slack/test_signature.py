"""`krater.slack.signature.verify_slack_signature`: valid, bad, stale and tampered requests."""

from __future__ import annotations

import hashlib
import hmac
import time

from krater.slack.signature import MAX_TIMESTAMP_AGE_SECONDS, verify_slack_signature

SECRET = "test-signing-secret"
BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    base_string = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_valid_signature_verifies() -> None:
    timestamp = str(int(time.time()))
    signature = _sign(SECRET, timestamp, BODY)
    assert verify_slack_signature(signing_secret=SECRET, timestamp=timestamp, body=BODY, signature=signature)


def test_bad_signature_is_rejected() -> None:
    timestamp = str(int(time.time()))
    assert not verify_slack_signature(signing_secret=SECRET, timestamp=timestamp, body=BODY, signature="v0=" + "0" * 64)


def test_stale_timestamp_is_rejected() -> None:
    timestamp = str(int(time.time()) - MAX_TIMESTAMP_AGE_SECONDS - 60)
    signature = _sign(SECRET, timestamp, BODY)
    assert not verify_slack_signature(signing_secret=SECRET, timestamp=timestamp, body=BODY, signature=signature)


def test_tampered_body_is_rejected() -> None:
    timestamp = str(int(time.time()))
    signature = _sign(SECRET, timestamp, BODY)
    tampered = BODY + b"extra"
    assert not verify_slack_signature(signing_secret=SECRET, timestamp=timestamp, body=tampered, signature=signature)


def test_missing_signing_secret_is_rejected() -> None:
    timestamp = str(int(time.time()))
    signature = _sign(SECRET, timestamp, BODY)
    assert not verify_slack_signature(signing_secret="", timestamp=timestamp, body=BODY, signature=signature)


def test_missing_headers_are_rejected() -> None:
    assert not verify_slack_signature(signing_secret=SECRET, timestamp=None, body=BODY, signature="v0=abc")
    timestamp = str(int(time.time()))
    assert not verify_slack_signature(signing_secret=SECRET, timestamp=timestamp, body=BODY, signature=None)
