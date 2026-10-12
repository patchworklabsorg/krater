"""`krater.web.logging_config`: secrets never make it into a formatted log line.

Covers the scrubber directly, and the specific case the hardening pass called out: a wrong/valid SkyPilot
policy token in the URL query string (as it would appear in a raw uvicorn access log line) never survives
scrubbing.
"""

from __future__ import annotations

import io
import json
import logging

from krater.config import Settings
from krater.web.logging_config import JsonFormatter, RequestIdFilter, ScrubbingFilter, request_id_var, scrub


def test_scrub_redacts_a_query_string_token() -> None:
    line = 'POST /internal/skypilot/policy?token=abcdef0123456789 HTTP/1.1" 200'

    scrubbed = scrub(line)

    assert "abcdef0123456789" not in scrubbed
    assert "token=***" in scrubbed


def test_scrub_redacts_slack_signature_header() -> None:
    line = "X-Slack-Signature: v0=deadbeefcafebabe1234567890"

    scrubbed = scrub(line)

    assert "deadbeefcafebabe1234567890" not in scrubbed


def test_scrub_redacts_cookie_and_session_values() -> None:
    line = "Cookie: krater_session=eyJhbGciOiJI.some.signed.value; other=1"

    scrubbed = scrub(line)

    assert "eyJhbGciOiJI" not in scrubbed


def test_scrub_redacts_secret_and_key_style_settings() -> None:
    for line in [
        "weave_client_secret=super-secret-value",
        "weave_service_key: wk_live_abc123",
        "s3_access_key_id=AKIAEXAMPLE",
        "s3_secret_access_key=abc/DEF+ghi",
        "Authorization: Bearer sk-abcdefg",
        "slack_signing_secret=shhh",
    ]:
        scrubbed = scrub(line)
        # Everything after the separator for a sensitive key is gone -- assert on the *value* fragments
        # specifically, since the key names themselves are meant to stay (they're what makes a log line
        # useful at all).
        for needle in ("super-secret-value", "wk_live_abc123", "AKIAEXAMPLE", "abc/DEF+ghi", "sk-abcdefg", "shhh"):
            assert needle not in scrubbed


def test_scrub_leaves_ordinary_text_alone() -> None:
    line = "GET /projects/123 HTTP/1.1 200"

    assert scrub(line) == line


def test_scrub_survives_percent_signs_in_the_redacted_text() -> None:
    """A query string with URL-encoded characters (`%20`, `%3D`, ...) next to a token must not blow up
    `%`-style log formatting once scrubbed -- see `ScrubbingFilter`'s docstring on why `args` gets
    cleared."""
    line = "GET /callback?token=abc%20def&next=%2Fsomewhere HTTP/1.1"

    scrubbed = scrub(line)

    assert "abc%20def" not in scrubbed
    assert "%2Fsomewhere" in scrubbed  # unrelated query params are untouched


def test_scrubbing_filter_mutates_the_record_and_clears_args_for_ordinary_loggers() -> None:
    record = logging.LogRecord(
        name="krater.web.routers.skypilot_policy",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="skypilot launch blocked: %s (token=%s)",
        args=("cost cap exceeded", "verysecrettoken123"),
        exc_info=None,
    )

    assert ScrubbingFilter().filter(record) is True

    assert record.args == ()
    assert "verysecrettoken123" not in record.msg
    # No exception formatting the record even though it now contains a raw literal that would otherwise
    # be interpreted as a `%`-format spec (it isn't, since `args` is empty).
    assert record.getMessage() == record.msg


def test_scrubbing_filter_preserves_uvicorns_access_log_args_shape() -> None:
    """`uvicorn.access` records carry a fixed 5-tuple (client_addr, method, full_path, http_version,
    status_code) that uvicorn's own `AccessFormatter` unpacks positionally -- collapsing it to a plain
    string (as happens for every other logger) would make that unpacking crash on every request. Only
    `full_path` (index 2, where a query string like `?token=...` lives) gets scrubbed; the tuple survives
    intact."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:0", "POST", "/internal/skypilot/policy?token=verysecrettoken123", "1.1", 200),
        exc_info=None,
    )

    assert ScrubbingFilter().filter(record) is True

    assert len(record.args) == 5
    assert record.args[0] == "127.0.0.1:0"
    assert record.args[1] == "POST"
    assert "verysecrettoken123" not in record.args[2]
    assert record.args[3] == "1.1"
    assert record.args[4] == 200


def test_request_id_filter_reads_the_contextvar() -> None:
    token = request_id_var.set("req-123")
    try:
        record = logging.LogRecord("krater", logging.INFO, __file__, 1, "hello", (), None)
        RequestIdFilter().filter(record)
        assert record.request_id == "req-123"
    finally:
        request_id_var.reset(token)


def test_json_formatter_emits_valid_json_with_the_scrubbed_message() -> None:
    record = logging.LogRecord("krater.web", logging.WARNING, __file__, 1, "blocked with token=deadbeef", (), None)
    record.request_id = "req-abc"
    ScrubbingFilter().filter(record)

    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "krater.web"
    assert payload["request_id"] == "req-abc"
    assert "deadbeef" not in payload["message"]


def test_configure_logging_scrubs_uvicorn_access_log_query_strings() -> None:
    """End-to-end through the logging machinery `configure_logging` wires up, using uvicorn's *real*
    `AccessFormatter` (not a generic one) -- the exact shape it needs from `record.args` is precisely why
    the filter has to special-case it (see `ScrubbingFilter`). This is the SkyPilot policy route's own
    access log line, real token in the query string included, and it must neither show the token nor
    raise while formatting."""
    import uvicorn.logging

    from krater.web.logging_config import configure_logging

    configure_logging(Settings(env="development"))

    access_logger = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    test_handler = logging.StreamHandler(stream)
    test_handler.setFormatter(uvicorn.logging.AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    access_logger.addHandler(test_handler)
    try:
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:0",
            "POST",
            "/internal/skypilot/policy?token=supersecrettoken",
            "1.1",
            200,
        )
    finally:
        access_logger.removeHandler(test_handler)

    output = stream.getvalue()
    assert "supersecrettoken" not in output
    assert "/internal/skypilot/policy" in output
    assert "POST" in output and "200" in output
