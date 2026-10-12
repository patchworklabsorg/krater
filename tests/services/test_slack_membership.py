"""The Slack membership gate (`docs/SPEC.md` "Roles & authentication"). Weave's `slack_member` decides
when Weave gives it. Otherwise Slack decides about the Slack id Weave gave (stored on the user):
`users.info` must show an account that isn't deleted or a guest. Krater never looks Slack accounts up by
email: it keeps no emails (patchworklabsorg/krater#7)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.slack_membership import is_full_slack_member, slack_id_for
from krater.slack.errors import SlackUnavailableError
from krater.slack.fake import FakeSlackClient


class _RecordingSlackClient(FakeSlackClient):
    """Records `users.info` calls, and can be told to fail like a Slack outage."""

    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__()
        self.user_info_calls: list[str] = []
        self._error = error

    def get_user_info(self, slack_user_id: str):
        self.user_info_calls.append(slack_user_id)
        if self._error is not None:
            raise self._error
        return super().get_user_info(slack_user_id)


def test_a_stored_slack_id_of_a_full_member_passes(db_session: Session, make_user) -> None:
    user: User = make_user(slack_user_id="USTORED")
    slack_client = _RecordingSlackClient()

    assert is_full_slack_member(db_session, slack_client, user) is True
    assert slack_client.user_info_calls == ["USTORED"]


@pytest.mark.parametrize(
    "slack_state",
    [{"is_restricted": True}, {"is_ultra_restricted": True}, {"deleted": True}],
    ids=["restricted", "ultra-restricted", "deleted"],
)
def test_guests_and_deactivated_accounts_are_refused(db_session: Session, make_user, slack_state: dict) -> None:
    user: User = make_user(slack_user_id="USTORED")
    slack_client = _RecordingSlackClient()
    slack_client.set_user_info("USTORED", **slack_state)

    assert is_full_slack_member(db_session, slack_client, user) is False


def test_a_slack_id_unknown_to_slack_is_refused(db_session: Session, make_user) -> None:
    user: User = make_user(slack_user_id="USTORED")
    slack_client = _RecordingSlackClient()
    slack_client.remove_user_info("USTORED")

    assert is_full_slack_member(db_session, slack_client, user) is False


def test_without_a_slack_id_from_weave_there_is_no_slack_account(db_session: Session, make_user) -> None:
    user: User = make_user()
    slack_client = _RecordingSlackClient()

    assert slack_id_for(db_session, slack_client, user) is None
    assert is_full_slack_member(db_session, slack_client, user) is False
    assert slack_client.user_info_calls == []


def test_a_slack_outage_propagates_instead_of_deciding(db_session: Session, make_user) -> None:
    user: User = make_user(slack_user_id="USTORED")
    slack_client = _RecordingSlackClient(error=SlackUnavailableError("down"))

    with pytest.raises(SlackUnavailableError):
        is_full_slack_member(db_session, slack_client, user)


@pytest.mark.parametrize("weave_says", [True, False])
def test_weaves_slack_member_answer_wins_without_asking_slack(db_session: Session, make_user, weave_says: bool) -> None:
    user: User = make_user(slack_user_id="USTORED")
    slack_client = _RecordingSlackClient()
    slack_client.set_user_info("USTORED", is_ultra_restricted=True)

    assert is_full_slack_member(db_session, slack_client, user, weave_slack_member=weave_says) is weave_says
    assert slack_client.user_info_calls == []
