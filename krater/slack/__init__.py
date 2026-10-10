"""`krater.slack`: the only place that talks to Slack.

Nothing outside this package should know Slack's URLs, wire shapes or SDK error codes -- go through
`SlackClient` (via `get_slack_client`). See `docs/SPEC.md` "Slack integration" for the design and
`docs/dev/slack-setup.md` for how the app itself is set up.
"""

from __future__ import annotations

from functools import lru_cache

from krater.config import get_settings
from krater.slack.client import SlackClient
from krater.slack.errors import SlackError, SlackRequestFailedError, SlackUnavailableError
from krater.slack.fake import FakeSlackClient
from krater.slack.live import LiveSlackClient
from krater.slack.types import SlackUserInfo

__all__ = [
    "FakeSlackClient",
    "LiveSlackClient",
    "SlackClient",
    "SlackError",
    "SlackRequestFailedError",
    "SlackUnavailableError",
    "SlackUserInfo",
    "get_slack_client",
]


@lru_cache
def get_slack_client() -> SlackClient:
    """The process-wide `SlackClient`, chosen by `settings.slack_mode`.

    Cached like `krater.weave.get_weave_client`/`krater.skypilot.get_skypilot_client`; tests reach the
    same fake instance this returns to set up/assert on Slack state.
    """
    settings = get_settings()
    if settings.slack_mode == "fake":
        return FakeSlackClient()
    return LiveSlackClient(settings)
