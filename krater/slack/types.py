"""Data carried out of the Slack adapter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackUserInfo:
    """What `SlackClient.get_user_info` says about a Slack account right now.

    Used by the Slack membership gate (`krater.services.slack_membership`) -- see `docs/SPEC.md`
    "Roles & authentication" -- to refuse guests and deactivated accounts.
    """

    slack_id: str
    deleted: bool
    is_restricted: bool
    is_ultra_restricted: bool


__all__ = ["SlackUserInfo"]
