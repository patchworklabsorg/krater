"""The `SlackClient` protocol every adapter (live or fake) implements.

Nothing outside `krater.slack` should know Slack's URLs, wire shapes or SDK error codes -- go through
this interface. See `docs/SPEC.md` "Slack integration" for the design and `docs/dev/slack-setup.md`
for the app setup this was built against.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from krater.slack.types import SlackUserInfo


@runtime_checkable
class SlackClient(Protocol):
    def create_channel(self, name: str) -> str:
        """Create a new private channel named `name` and return its id.

        Idempotent in intent: if `name` is already taken by an existing (non-archived) channel, that
        channel's id is returned instead of raising -- callers still only call this once per project
        (they check `Project.slack_channel_id` first), but a channel can be created out-of-band too.
        """
        ...

    def invite_users(self, channel_id: str, slack_user_ids: list[str]) -> None:
        """Invite every id in `slack_user_ids` to `channel_id`. A no-op for an empty list.

        Safe to retry: a user who's already a member doesn't cause an error.
        """
        ...

    def archive_channel(self, channel_id: str) -> None:
        """Archive `channel_id`. Safe to call on a channel that's already archived."""
        ...

    def post_message(self, channel_id: str, *, blocks: list[dict], text: str) -> str:
        """Post a message to `channel_id` and return its `ts` (used to update it later).

        `text` is the plain-text fallback Slack shows in notifications/unfurls; `blocks` is the actual
        rendered content.
        """
        ...

    def update_message(self, channel_id: str, ts: str, *, blocks: list[dict], text: str) -> None:
        """Replace the content of the message at `ts` in `channel_id`."""
        ...

    def open_view(self, trigger_id: str, view: dict) -> None:
        """Open a modal (`views.open`). `trigger_id` is single-use and expires in ~3s of being
        issued, so this must be called synchronously from the interaction that produced it."""
        ...

    def post_ephemeral_via_response_url(self, response_url: str, text: str) -> None:
        """Post an ephemeral message back to whoever triggered an interaction, via its `response_url`.

        Used to report a domain error (e.g. "you can't review your own project") to the person who
        clicked, without anyone else in the channel seeing it.
        """
        ...

    def lookup_user_by_email(self, email: str) -> str | None:
        """The Slack user id for `email`, or `None` if no Slack account uses it."""
        ...

    def get_user_info(self, slack_user_id: str) -> SlackUserInfo | None:
        """Directory info for `slack_user_id`, or `None` if it doesn't exist."""
        ...


__all__ = ["SlackClient"]
