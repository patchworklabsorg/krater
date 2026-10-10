"""Logging setup: a request id on every log line, secrets scrubbed out, JSON in production.

`configure_logging` is called once from `krater.web.app.create_app`. It:

- attaches a `RequestIdFilter` (reads the `request_id` contextvar `RequestIdMiddleware` sets per request)
  and a `ScrubbingFilter` (redacts secrets -- see its docstring) to the root logger and to uvicorn's own
  `uvicorn.access`/`uvicorn.error` loggers. uvicorn configures those with `propagate=False` and their own
  handlers, so a filter on the root logger alone would never see their records -- see
  `docs/skypilot-integration.md`'s neighbor, `docs/SPEC.md` "Error handling", for why the access log in
  particular matters: it logs the full request line, query string included, and the SkyPilot policy
  route's `?token=...` lives right there.
- in production, formats the root logger's own output as one JSON object per line.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar

from krater.config import Settings

#: Set by `RequestIdMiddleware` for the duration of each request; read back by `RequestIdFilter` so every
#: log line emitted while handling a request carries the same id, without threading it through every
#: function call by hand.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_REQUEST_ID_HEADER = "X-Request-ID"

# --------------------------------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------------------------------

#: `key=value` or `key: value` pairs whose value must never reach a log line: OAuth/API tokens, Slack's
#: request signature, session cookies, and any of the various `*_key`/`*_secret` settings -- including
#: ones the keyword only appears *inside* of, like `weave_client_secret` or `s3_access_key_id`, which is why
#: `name` greedily grabs the whole surrounding identifier rather than just the bare keyword. Matched
#: case-insensitively; the value is redacted regardless of what it looks like. This is what keeps the
#: SkyPilot policy URL's `?token=...` (and Slack's `X-Slack-Signature`, `Cookie`, Weave's client secret,
#: S3's access/secret keys, etc.) out of both application logs and uvicorn's access log.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"""
    (?P<name>
        [A-Za-z0-9_-]*
        (?:token|secret|signature|password|passwd|key|cookie|session|authorization)
        [A-Za-z0-9_-]*
    )
    (?P<sep>\s*[:=]\s*)
    (?P<scheme>Bearer\s+)?
    (?P<value>[^\s&"',;]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_REDACTED = "***"


def scrub(text: str) -> str:
    """Redact every `key=value`/`key: value` pair in `text` whose key names a secret. Safe to call on
    arbitrary text (a log message, a query string, a header) -- it only ever redacts values, never drops
    or reorders anything else."""

    def _redact(match: re.Match[str]) -> str:
        scheme = match.group("scheme") or ""
        return f"{match.group('name')}{match.group('sep')}{scheme}{_REDACTED}"

    return _SENSITIVE_KEY_PATTERN.sub(_redact, text)


#: uvicorn's `AccessFormatter.formatMessage` (uvicorn/logging.py) bypasses the normal `msg % args`
#: machinery entirely: it unpacks `record.args` itself, positionally, as exactly
#: `(client_addr, method, full_path, http_version, status_code)`. Collapsing that into a plain string (our
#: usual approach below) would make its unpacking crash on every single access log line -- so an
#: `uvicorn.access` record with that exact shape gets its `full_path` element scrubbed in place instead,
#: preserving the tuple uvicorn's formatter expects. `full_path` is where the SkyPilot policy route's
#: `?token=...` lives.
_UVICORN_ACCESS_ARGS_LENGTH = 5
_UVICORN_ACCESS_FULL_PATH_INDEX = 2


class ScrubbingFilter(logging.Filter):
    """Rewrites a record's message text to its scrubbed form.

    For most loggers that means collapsing to the fully-formatted, scrubbed text in `record.msg` and
    clearing `record.args`: `LogRecord.getMessage()` only applies `%`-interpolation when `args` is truthy,
    so once we've done the interpolation ourselves and stashed the scrubbed result in `msg`, `args` must
    be cleared too -- otherwise a scrubbed message that happens to contain a literal `%` (common in
    URL-encoded query strings) would blow up formatting a second time. `uvicorn.access` records are
    handled differently -- see `_UVICORN_ACCESS_ARGS_LENGTH` above.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) == _UVICORN_ACCESS_ARGS_LENGTH
        ):
            args = list(record.args)
            args[_UVICORN_ACCESS_FULL_PATH_INDEX] = scrub(str(args[_UVICORN_ACCESS_FULL_PATH_INDEX]))
            record.args = tuple(args)
            return True

        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


class RequestIdFilter(logging.Filter):
    """Attaches the current request's id (if any) to every record, as `record.request_id`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger name, the (already-scrubbed) message, the
    request id, and exception info when present."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_PLAIN_FORMAT = "%(asctime)s %(levelname)s %(name)s [request_id=%(request_id)s]: %(message)s"

#: Loggers uvicorn owns and configures itself (handlers included) before/around app startup -- our
#: filters are added directly to the logger objects, which still runs ahead of their own handlers
#: regardless of `propagate`. See the module docstring.
_UVICORN_LOGGER_NAMES = ("uvicorn.access", "uvicorn.error")


def _replace_filters_of_type(logger: logging.Logger, *new_filters: logging.Filter) -> None:
    """Add each of `new_filters`, first removing any existing filter of the same type -- so calling
    `configure_logging` more than once (as every `create_app()` call does, including once per test
    client) doesn't pile up duplicate filters on loggers this module doesn't own outright, like
    uvicorn's."""
    for new_filter in new_filters:
        for existing in list(logger.filters):
            if type(existing) is type(new_filter):
                logger.removeFilter(existing)
        logger.addFilter(new_filter)


def configure_logging(settings: Settings) -> None:
    scrubber = ScrubbingFilter()
    request_id_filter = RequestIdFilter()

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if settings.env == "production" else logging.Formatter(_PLAIN_FORMAT))
    handler.addFilter(scrubber)
    handler.addFilter(request_id_filter)
    root.handlers = [handler]

    for name in _UVICORN_LOGGER_NAMES:
        _replace_filters_of_type(logging.getLogger(name), scrubber, request_id_filter)


def new_request_id() -> str:
    return uuid.uuid4().hex


__all__ = [
    "JsonFormatter",
    "RequestIdFilter",
    "ScrubbingFilter",
    "configure_logging",
    "new_request_id",
    "request_id_var",
    "scrub",
]
