"""Exception types raised by `krater.slack`.

Callers branch on these two, mirroring `krater.weave.errors` and `krater.skypilot.errors`: one for
"couldn't even reach or complete the round trip with Slack" (network error, timeout), one for "Slack
was reached and understood the request, but it failed" (an API error we can't treat as a harmless,
already-in-that-state retry, e.g. `already_in_channel`/`already_archived`/`name_taken`).
"""

from __future__ import annotations


class SlackError(Exception):
    """Base class for every error raised by `krater.slack`."""


class SlackUnavailableError(SlackError):
    """Slack couldn't be reached at all: a network error or a timeout."""


class SlackRequestFailedError(SlackError):
    """Slack was reached and understood the request, but it failed. Carries Slack's own error code/
    message so callers (and the worker's retry logic) can show or reason about it."""


__all__ = ["SlackError", "SlackRequestFailedError", "SlackUnavailableError"]
