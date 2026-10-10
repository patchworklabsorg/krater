"""`LiveSlackClient`: `SlackClient` backed by the real Slack Web API, via `slack_sdk`.

Every call that's meant to be idempotent (`create_channel`, `invite_users`, `archive_channel`) treats
the Slack error code that means "already in the state we wanted" as success, per `docs/SPEC.md`
("Invites and posts must be safe to retry: `already_in_channel` counts as success...").

`SlackApiError` (Slack understood the request but it failed) and the broader `SlackClientError` (a
network/transport problem, e.g. `SlackRequestError`) are handled separately -- `SlackApiError` is
itself a `SlackClientError`, so it's always caught first.
"""

from __future__ import annotations

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.webhook import WebhookClient

from krater.config import Settings
from krater.slack.errors import SlackRequestFailedError, SlackUnavailableError
from krater.slack.types import SlackUserInfo

logger = logging.getLogger(__name__)

#: `conversations.invite`/`conversations.archive` error codes that mean "already done" -- treated as
#: success so a retried job (or a channel someone joined manually in between) doesn't fail.
_ALREADY_IN_CHANNEL_ERRORS = frozenset({"already_in_channel"})
_ALREADY_ARCHIVED_ERRORS = frozenset({"already_archived"})

#: Per-user `conversations.invite` failures explained by that one person's account (already in, a
#: guest Slack won't add here, a deactivated or unknown id), as opposed to the channel or the app
#: being wrong. Those people are skipped and logged; anything else still fails the call.
_SKIPPABLE_INVITE_ERRORS = _ALREADY_IN_CHANNEL_ERRORS | frozenset(
    {"cant_invite", "cant_invite_self", "user_is_restricted", "ura_max_channels", "user_not_found"}
)


def _slack_error_code(exc: SlackApiError) -> str | None:
    try:
        return exc.response.get("error")
    except AttributeError:
        return None


def _request_failed(method: str, exc: SlackApiError) -> SlackRequestFailedError:
    return SlackRequestFailedError(f"{method} failed: {_slack_error_code(exc) or exc}")


def _unavailable(method: str, exc: SlackClientError) -> SlackUnavailableError:
    return SlackUnavailableError(f"could not reach Slack for {method}: {exc}")


def _per_user_invite_errors(exc: SlackApiError) -> dict[str, str]:
    """`conversations.invite`'s per-user failures as `{user_id: error_code}`, from the response's
    `errors` array (`[{"user": ..., "ok": false, "error": ...}]`). A response without one (a single
    invitee, typically) is attributed to `"?"` with the top-level code, so it's judged the same way."""
    try:
        errors = exc.response.get("errors")
    except AttributeError:
        errors = None
    if isinstance(errors, list) and errors:
        return {str(entry.get("user", "?")): str(entry.get("error")) for entry in errors if isinstance(entry, dict)}
    code = _slack_error_code(exc)
    return {"?": code} if code else {}


class LiveSlackClient:
    """A `SlackClient` (see `krater.slack.client`) backed by a real Slack workspace. `web_client` is
    injectable for tests; production code leaves it out and gets a real `slack_sdk.WebClient`."""

    def __init__(self, settings: Settings, *, web_client: WebClient | None = None) -> None:
        self._settings = settings
        self._client = web_client if web_client is not None else WebClient(token=settings.slack_bot_token)

    # -- Channels --------------------------------------------------------------------------------------

    def create_channel(self, name: str) -> str:
        try:
            response = self._client.conversations_create(name=name, is_private=True)
        except SlackApiError as exc:
            if _slack_error_code(exc) == "name_taken":
                existing = self._find_channel_by_name(name)
                if existing is not None:
                    return existing
            raise _request_failed("conversations.create", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("conversations.create", exc) from exc
        return response["channel"]["id"]

    def _find_channel_by_name(self, name: str) -> str | None:
        """Best-effort lookup for the `name_taken` case: someone (or a previous, half-finished attempt)
        already created a channel with this name. Paginates through every private channel Krater's bot
        can see."""
        cursor: str | None = None
        while True:
            try:
                response = self._client.conversations_list(
                    types="private_channel", exclude_archived=True, limit=200, cursor=cursor or None
                )
            except SlackApiError as exc:
                raise _request_failed("conversations.list", exc) from exc
            except SlackClientError as exc:
                raise _unavailable("conversations.list", exc) from exc
            for channel in response.get("channels", []):
                if channel.get("name") == name:
                    return channel["id"]
            cursor = response.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return None

    def invite_users(self, channel_id: str, slack_user_ids: list[str]) -> None:
        """Invite everyone in `slack_user_ids` who can be invited.

        Without `force`, Slack invites nobody if any one invite fails, and re-inviting a channel's
        team always includes people already in it (`already_in_channel`), so new reviewers or
        builders were silently never added. `force=True` makes Slack invite the valid ones anyway;
        per-user failures in `_SKIPPABLE_INVITE_ERRORS` are then logged and ignored.
        """
        if not slack_user_ids:
            return
        try:
            self._client.conversations_invite(channel=channel_id, users=slack_user_ids, force=True)
        except SlackApiError as exc:
            failures = _per_user_invite_errors(exc)
            if not failures or not set(failures.values()) <= _SKIPPABLE_INVITE_ERRORS:
                raise _request_failed("conversations.invite", exc) from exc
            skipped = {user: code for user, code in failures.items() if code not in _ALREADY_IN_CHANNEL_ERRORS}
            if skipped:
                logger.warning("Slack wouldn't invite %s to channel %s; invited everyone else", skipped, channel_id)
        except SlackClientError as exc:
            raise _unavailable("conversations.invite", exc) from exc

    def archive_channel(self, channel_id: str) -> None:
        try:
            self._client.conversations_archive(channel=channel_id)
        except SlackApiError as exc:
            if _slack_error_code(exc) in _ALREADY_ARCHIVED_ERRORS:
                return
            raise _request_failed("conversations.archive", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("conversations.archive", exc) from exc

    # -- Messages --------------------------------------------------------------------------------------

    def post_message(self, channel_id: str, *, blocks: list[dict], text: str) -> str:
        try:
            response = self._client.chat_postMessage(channel=channel_id, blocks=blocks, text=text)
        except SlackApiError as exc:
            raise _request_failed("chat.postMessage", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("chat.postMessage", exc) from exc
        return response["ts"]

    def update_message(self, channel_id: str, ts: str, *, blocks: list[dict], text: str) -> None:
        try:
            self._client.chat_update(channel=channel_id, ts=ts, blocks=blocks, text=text)
        except SlackApiError as exc:
            raise _request_failed("chat.update", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("chat.update", exc) from exc

    def open_view(self, trigger_id: str, view: dict) -> None:
        try:
            self._client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as exc:
            raise _request_failed("views.open", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("views.open", exc) from exc

    def post_ephemeral_via_response_url(self, response_url: str, text: str) -> None:
        try:
            response = WebhookClient(response_url).send(text=text, response_type="ephemeral")
        except SlackClientError as exc:
            raise _unavailable("response_url", exc) from exc
        if response.status_code >= 400:
            raise SlackRequestFailedError(f"response_url post failed: {response.status_code} {response.body}")

    # -- Directory -------------------------------------------------------------------------------------

    def lookup_user_by_email(self, email: str) -> str | None:
        try:
            response = self._client.users_lookupByEmail(email=email)
        except SlackApiError as exc:
            if _slack_error_code(exc) == "users_not_found":
                return None
            raise _request_failed("users.lookupByEmail", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("users.lookupByEmail", exc) from exc
        return response["user"]["id"]

    def get_user_info(self, slack_user_id: str) -> SlackUserInfo | None:
        try:
            response = self._client.users_info(user=slack_user_id)
        except SlackApiError as exc:
            if _slack_error_code(exc) == "user_not_found":
                return None
            raise _request_failed("users.info", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("users.info", exc) from exc
        user = response["user"]
        email = (user.get("profile") or {}).get("email")
        return SlackUserInfo(
            slack_id=user["id"],
            deleted=bool(user.get("deleted", False)),
            is_restricted=bool(user.get("is_restricted", False)),
            is_ultra_restricted=bool(user.get("is_ultra_restricted", False)),
            email=email if isinstance(email, str) and email else None,
        )


__all__ = ["LiveSlackClient"]
