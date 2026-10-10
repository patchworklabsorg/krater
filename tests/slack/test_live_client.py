"""`LiveSlackClient` against a stubbed `slack_sdk.WebClient`: how `invite_users` copes with
`conversations.invite`'s all-or-nothing behavior and its per-user failures, and what the directory calls
(`users.info`, `users.lookupByEmail`) return."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError, SlackRequestError

from krater.config import Settings
from krater.slack.errors import SlackRequestFailedError, SlackUnavailableError
from krater.slack.live import LiveSlackClient


class _InviteWebClient:
    """Records `conversations_invite` calls and raises whatever the test sets up."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def conversations_invite(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ok": True}


def _api_error(code: str, errors: list[dict] | None = None) -> SlackApiError:
    response: dict[str, Any] = {"ok": False, "error": code}
    if errors is not None:
        response["errors"] = errors
    return SlackApiError(message=code, response=response)


def _client(web_client: _InviteWebClient) -> LiveSlackClient:
    return LiveSlackClient(Settings(slack_bot_token="xoxb-test"), web_client=web_client)  # type: ignore[arg-type]


def test_invites_with_force_so_one_bad_invitee_cant_block_the_rest() -> None:
    web_client = _InviteWebClient()

    _client(web_client).invite_users("C1", ["U1", "U2"])

    assert web_client.calls == [{"channel": "C1", "users": ["U1", "U2"], "force": True}]


def test_does_nothing_for_an_empty_list() -> None:
    web_client = _InviteWebClient()

    _client(web_client).invite_users("C1", [])

    assert web_client.calls == []


def test_people_already_in_the_channel_are_fine() -> None:
    error = _api_error("already_in_channel", [{"user": "U1", "ok": False, "error": "already_in_channel"}])

    _client(_InviteWebClient(error)).invite_users("C1", ["U1", "U2"])


def test_a_single_invitee_already_in_the_channel_is_fine() -> None:
    _client(_InviteWebClient(_api_error("already_in_channel"))).invite_users("C1", ["U1"])


def test_guests_and_stale_ids_are_skipped_and_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _api_error(
        "cant_invite",
        [
            {"user": "U_GUEST", "ok": False, "error": "user_is_restricted"},
            {"user": "U_FULLGUEST", "ok": False, "error": "ura_max_channels"},
            {"user": "U_GONE", "ok": False, "error": "user_not_found"},
            {"user": "U_IN", "ok": False, "error": "already_in_channel"},
        ],
    )
    client = _client(_InviteWebClient(error))
    # Alembic's logging setup (run by the test session's migration) disables loggers that already exist.
    monkeypatch.setattr(logging.getLogger("krater.slack.live"), "disabled", False)

    with caplog.at_level(logging.WARNING, logger="krater.slack.live"):
        client.invite_users("C1", ["U_GUEST", "U_FULLGUEST", "U_GONE", "U_IN", "U_OK"])

    assert "U_GUEST" in caplog.text
    assert "U_GONE" in caplog.text
    assert "U_IN" not in caplog.text


def test_a_channel_level_failure_still_raises() -> None:
    error = _api_error("is_archived")

    with pytest.raises(SlackRequestFailedError, match="is_archived"):
        _client(_InviteWebClient(error)).invite_users("C1", ["U1", "U2"])


def test_a_per_user_failure_it_cant_explain_still_raises() -> None:
    error = _api_error(
        "no_permission",
        [
            {"user": "U1", "ok": False, "error": "already_in_channel"},
            {"user": "U2", "ok": False, "error": "no_permission"},
        ],
    )

    with pytest.raises(SlackRequestFailedError):
        _client(_InviteWebClient(error)).invite_users("C1", ["U1", "U2"])


def test_a_network_error_is_unavailable() -> None:
    with pytest.raises(SlackUnavailableError):
        _client(_InviteWebClient(SlackRequestError("boom"))).invite_users("C1", ["U1"])


class _DirectoryWebClient:
    """Answers `users_info` / `users_lookupByEmail` with a canned user, or raises `error`."""

    def __init__(self, user: dict | None = None, error: Exception | None = None) -> None:
        self.user = user
        self.error = error

    def users_info(self, **kwargs: Any) -> dict:
        if self.error is not None:
            raise self.error
        return {"ok": True, "user": self.user}

    def users_lookupByEmail(self, **kwargs: Any) -> dict:  # noqa: N802 -- slack_sdk's method name
        if self.error is not None:
            raise self.error
        return {"ok": True, "user": self.user}


@pytest.mark.parametrize(
    "profile,expected",
    [({"email": "ada@example.com"}, "ada@example.com"), ({}, None), ({"email": ""}, None)],
    ids=["email", "no-email", "blank-email"],
)
def test_get_user_info_reads_the_guest_flags_and_the_profile_email(profile: dict, expected: str | None) -> None:
    user = {"id": "U1", "deleted": False, "is_restricted": True, "is_ultra_restricted": False, "profile": profile}

    info = _client(_DirectoryWebClient(user)).get_user_info("U1")  # type: ignore[arg-type]

    assert info is not None
    assert info.is_restricted is True
    assert info.email == expected


def test_get_user_info_is_none_for_an_unknown_user() -> None:
    assert _client(_DirectoryWebClient(error=_api_error("user_not_found"))).get_user_info("U404") is None  # type: ignore[arg-type]


def test_lookup_user_by_email_returns_the_id_or_none() -> None:
    assert _client(_DirectoryWebClient({"id": "U7"})).lookup_user_by_email("a@example.com") == "U7"  # type: ignore[arg-type]
    missing = _client(_DirectoryWebClient(error=_api_error("users_not_found")))  # type: ignore[arg-type]
    assert missing.lookup_user_by_email("nobody@example.com") is None
