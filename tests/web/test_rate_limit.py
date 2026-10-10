"""`krater.web.rate_limit`: the token bucket itself, trusted-proxy IP resolution, and the 429s the
middleware returns once a bucket runs dry.

The middleware is a no-op under `KRATER_ENV=test` (see its module docstring) so the rest of the suite's
heavy use of `/login`/`/auth/callback` and POSTs doesn't trip it. These tests flip `env` to `"development"`
for the duration of each case and reset the shared bucket table first, so cases don't leak quota into one
another (or into the rest of the suite, since `reset_rate_limits()` runs again on the next case's setup).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from krater.config import Settings, get_settings
from krater.web.rate_limit import (
    RateLimit,
    _take_token,
    get_client_ip,
    reset_rate_limits,
)


@pytest.fixture(autouse=True)
def _clean_buckets():
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture
def rate_limited(client: TestClient, monkeypatch):
    """`client`, with the rate limiter actually switched on for this test."""
    monkeypatch.setattr(get_settings(), "env", "development")
    return client


# --------------------------------------------------------------------------------------------------
# The bucket itself
# --------------------------------------------------------------------------------------------------


def test_bucket_allows_up_to_capacity_then_blocks() -> None:
    limit = RateLimit(capacity=3, per_seconds=60)
    now = 1000.0

    for _ in range(3):
        assert _take_token("g", "k", limit, now=now) is None

    retry_after = _take_token("g", "k", limit, now=now)
    assert retry_after is not None
    assert retry_after > 0


def test_bucket_refills_over_time() -> None:
    limit = RateLimit(capacity=1, per_seconds=10)
    now = 1000.0

    assert _take_token("g", "k2", limit, now=now) is None
    assert _take_token("g", "k2", limit, now=now) is not None  # empty already

    # A full refill period later, a token is available again.
    assert _take_token("g", "k2", limit, now=now + 10) is None


def test_different_keys_have_independent_buckets() -> None:
    limit = RateLimit(capacity=1, per_seconds=60)
    now = 1000.0

    assert _take_token("g", "a", limit, now=now) is None
    assert _take_token("g", "b", limit, now=now) is None  # not affected by "a"'s consumption
    assert _take_token("g", "a", limit, now=now) is not None


# --------------------------------------------------------------------------------------------------
# Trusted-proxy IP resolution
# --------------------------------------------------------------------------------------------------


def _fake_request(*, xff: str | None, peer: str = "10.0.0.9") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    scope = {
        "type": "http",
        "headers": headers,
        "client": (peer, 12345),
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


def test_untrusted_proxy_count_ignores_x_forwarded_for() -> None:
    settings = Settings(trusted_proxy_count=0)
    request = _fake_request(xff="1.2.3.4, 5.6.7.8")

    assert get_client_ip(request, settings) == "10.0.0.9"


def test_trusted_proxy_count_one_reads_the_right_hop() -> None:
    settings = Settings(trusted_proxy_count=1)
    # One trusted proxy in front of us: it appended the client's address before forwarding, so the
    # header is "<client>, <proxy> is not present -- only what arrived at proxy, which is just the
    # client" -- i.e. with exactly 1 trusted hop and a 1-entry header, the entry itself is the client.
    request = _fake_request(xff="203.0.113.5")

    assert get_client_ip(request, settings) == "203.0.113.5"


def test_trusted_proxy_count_two_reads_the_right_hop() -> None:
    settings = Settings(trusted_proxy_count=2)
    # Two trusted proxies in front of us, each appending one hop: "<client>, <first proxy's address>".
    request = _fake_request(xff="198.51.100.7, 10.0.0.1")

    assert get_client_ip(request, settings) == "198.51.100.7"


def test_a_forged_header_prefix_is_ignored() -> None:
    """A client can send whatever `X-Forwarded-For` prefix it likes -- with `trusted_proxy_count=1`, only
    the *single* trailing hop (the one our one trusted proxy itself appended, recording who actually
    connected to it) is honored. Everything the attacker prepended is discarded, not read as if it were
    trusted infrastructure."""
    settings = Settings(trusted_proxy_count=1)
    request = _fake_request(xff="1.1.1.1, 2.2.2.2, 3.3.3.3")

    assert get_client_ip(request, settings) == "3.3.3.3"


def test_missing_header_falls_back_to_peer_even_when_proxies_are_trusted() -> None:
    settings = Settings(trusted_proxy_count=2)
    request = _fake_request(xff=None)

    assert get_client_ip(request, settings) == "10.0.0.9"


def test_header_shorter_than_trusted_count_falls_back_to_peer() -> None:
    settings = Settings(trusted_proxy_count=3)
    request = _fake_request(xff="9.9.9.9")

    assert get_client_ip(request, settings) == "10.0.0.9"


# --------------------------------------------------------------------------------------------------
# End to end: the middleware actually returns 429s, with Retry-After, once its group is exhausted.
# --------------------------------------------------------------------------------------------------


def test_disabled_under_the_test_env_by_default(client: TestClient) -> None:
    # No `rate_limited` fixture here -- default `KRATER_ENV=test` -- so hammering /login is harmless.
    for _ in range(50):
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302


def test_login_returns_429_with_retry_after_once_exhausted(rate_limited: TestClient) -> None:
    statuses = [rate_limited.get("/login", follow_redirects=False).status_code for _ in range(30)]

    assert 429 in statuses
    limited = rate_limited.get("/login", follow_redirects=False)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 0


def test_post_general_is_rate_limited_too(rate_limited: TestClient, login_as, create_project):
    from tests.conftest import MEMBER_SUB

    user = login_as(MEMBER_SUB)
    project = create_project(user)
    edit_page = rate_limited.get(f"/projects/{project.id}/edit").text
    csrf_token = edit_page.split('name="csrf_token" value="')[1].split('"')[0]

    statuses = []
    for _ in range(150):
        response = rate_limited.post(
            f"/projects/{project.id}/edit",
            data={"csrf_token": csrf_token, "title": "Still a draft", "write_up": "..."},
            follow_redirects=False,
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            break

    assert 429 in statuses
    assert int(rate_limited.post(f"/projects/{project.id}/edit", data={}).headers["Retry-After"]) >= 0


def test_rate_limit_key_is_per_ip_not_global(rate_limited: TestClient, monkeypatch) -> None:
    """Exhausting the login bucket under one `X-Forwarded-For` client doesn't affect a different one --
    proof the limiter really is keyed, not a single global counter -- using `trusted_proxy_count` so the
    header is actually honored."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 1)

    for _ in range(20):
        rate_limited.get("/login", headers={"X-Forwarded-For": "1.1.1.1"}, follow_redirects=False)
    exhausted = rate_limited.get("/login", headers={"X-Forwarded-For": "1.1.1.1"}, follow_redirects=False)
    fresh = rate_limited.get("/login", headers={"X-Forwarded-For": "2.2.2.2"}, follow_redirects=False)

    assert exhausted.status_code == 429
    assert fresh.status_code == 302
