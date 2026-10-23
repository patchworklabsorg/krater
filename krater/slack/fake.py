"""`FakeSlackClient`: an in-memory `SlackClient` for `KRATER_SLACK_MODE=fake` (development and tests).

Makes no network calls. Every mutating call is recorded (`self.posts`, `self.updates`, ...) so tests
can assert on what happened. Any Slack id answers `get_user_info` as a full, active member by default,
so the Slack membership gate doesn't block ordinary dev/test flows for the `stub_users.json` fixture
users (whose `slack_id` stub sign-in stores). The one exception is the fixture's guest,
`DEV_GUEST_SLACK_ID`, which answers as a single-channel guest so the gate can be seen working in dev.
"""

from __future__ import annotations

import itertools

from krater.slack.types import SlackUserInfo

#: `PWLSLACKGUEST`'s `slack_id` in `krater/weave/stub_users.json`.
DEV_GUEST_SLACK_ID = "U0005GUEST"


class FakeSlackClient:
    """A `SlackClient` backed by plain Python dicts."""

    def __init__(self) -> None:
        # channel_id -> {"name": str, "members": set[str], "archived": bool}
        self.channels: dict[str, dict] = {}
        self._channel_id_seq = itertools.count(1)
        # (channel_id, ts) -> {"blocks": list[dict], "text": str}
        self.messages: dict[tuple[str, str], dict] = {}
        self._ts_seq = itertools.count(1)
        self.opened_views: list[dict] = []
        self.ephemeral_messages: list[tuple[str, str]] = []  # (response_url, text)
        # slack_id -> SlackUserInfo override; any id not listed here answers as a full active member.
        self._user_info: dict[str, SlackUserInfo] = {
            DEV_GUEST_SLACK_ID: SlackUserInfo(
                slack_id=DEV_GUEST_SLACK_ID, deleted=False, is_restricted=False, is_ultra_restricted=True
            )
        }
        # ids that should answer `None` from `get_user_info`, as for an unknown/deleted-from-Slack id.
        self._missing_user_ids: set[str] = set()

    # -- SlackClient protocol ------------------------------------------------------------------------

    def create_channel(self, name: str) -> str:
        for channel_id, channel in self.channels.items():
            if channel["name"] == name and not channel["archived"]:
                return channel_id
        channel_id = f"C{next(self._channel_id_seq):09d}"
        self.channels[channel_id] = {"name": name, "members": set(), "archived": False}
        return channel_id

    def invite_users(self, channel_id: str, slack_user_ids: list[str]) -> None:
        if not slack_user_ids:
            return
        self.channels[channel_id]["members"].update(slack_user_ids)

    def remove_user(self, channel_id: str, slack_user_id: str) -> None:
        self.channels[channel_id]["members"].discard(slack_user_id)

    def archive_channel(self, channel_id: str) -> None:
        self.channels[channel_id]["archived"] = True

    def post_message(self, channel_id: str, *, blocks: list[dict], text: str) -> str:
        ts = f"{next(self._ts_seq)}.000000"
        self.messages[(channel_id, ts)] = {"blocks": blocks, "text": text}
        return ts

    def update_message(self, channel_id: str, ts: str, *, blocks: list[dict], text: str) -> None:
        self.messages[(channel_id, ts)] = {"blocks": blocks, "text": text}

    def open_view(self, trigger_id: str, view: dict) -> None:
        self.opened_views.append({"trigger_id": trigger_id, "view": view})

    def post_ephemeral_via_response_url(self, response_url: str, text: str) -> None:
        self.ephemeral_messages.append((response_url, text))

    def get_user_info(self, slack_user_id: str) -> SlackUserInfo | None:
        if slack_user_id in self._missing_user_ids:
            return None
        if slack_user_id in self._user_info:
            return self._user_info[slack_user_id]
        # Unknown ids answer as a full, active member -- see module docstring.
        return SlackUserInfo(slack_id=slack_user_id, deleted=False, is_restricted=False, is_ultra_restricted=False)

    # -- Test helpers ----------------------------------------------------------------------------------

    def set_user_info(
        self,
        slack_user_id: str,
        *,
        deleted: bool = False,
        is_restricted: bool = False,
        is_ultra_restricted: bool = False,
    ) -> None:
        """Override what `get_user_info(slack_user_id)` answers, e.g. to simulate a guest or a
        deactivated account. `unset_user_info` (or a missing id) restores the "full member" default."""
        self._user_info[slack_user_id] = SlackUserInfo(
            slack_id=slack_user_id,
            deleted=deleted,
            is_restricted=is_restricted,
            is_ultra_restricted=is_ultra_restricted,
        )

    def unset_user_info(self, slack_user_id: str) -> None:
        """Undo `set_user_info`/`remove_user_info`, restoring the "full active member" default."""
        self._user_info.pop(slack_user_id, None)
        self._missing_user_ids.discard(slack_user_id)

    def remove_user_info(self, slack_user_id: str) -> None:
        """Make `get_user_info(slack_user_id)` answer `None`, as for an unknown/deleted-from-Slack id."""
        self._missing_user_ids.add(slack_user_id)


__all__ = ["DEV_GUEST_SLACK_ID", "FakeSlackClient"]
