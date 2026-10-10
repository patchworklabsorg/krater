"""Data carried out of the Slack adapter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackUserInfo:
    """What `SlackClient.get_user_info` says about a Slack account right now.

    Used by the Slack membership gate (`krater.services.slack_membership`) -- see `docs/SPEC.md`
    "Roles & authentication" -- to refuse guests and deactivated accounts, and by the Slack click path
    (`krater.services.slack_reviews`) to match an unlinked clicker to a Krater user by `email` (needs the
    `users:read.email` scope; `None` without it or when the profile has none).
    """

    slack_id: str
    deleted: bool
    is_restricted: bool
    is_ultra_restricted: bool
    email: str | None = None


__all__ = ["SlackUserInfo"]
