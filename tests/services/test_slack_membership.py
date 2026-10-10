"""The Slack membership gate (`docs/SPEC.md` "Roles & authentication"). Weave's `slack_member` decides
when Weave gives it. Otherwise Slack decides: the user's stored Slack id, else `users.lookupByEmail`
with their verified email (cached), then `users.info` must show an account that isn't deleted or a
guest."""

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


def test_a_stored_slack_id_of_a_full_member_passes_without_an_email_lookup(db_session: Session, make_user) -> None:
    user: User = make_user(slack_user_id="USTORED")
    slack_client = _RecordingSlackClient()

    assert is_full_slack_member(db_session, slack_client, user) is True
    assert slack_client.user_info_calls == ["USTORED"]
    assert slack_client.email_lookups == []


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


def test_without_a_stored_id_it_looks_up_the_verified_email_and_caches_the_id(db_session: Session, make_user) -> None:
    user: User = make_user(email="Found@Example.com")
    slack_client = _RecordingSlackClient()
    slack_client.register_email("found@example.com", "UBYEMAIL")

    assert is_full_slack_member(db_session, slack_client, user) is True
    assert slack_client.user_info_calls == ["UBYEMAIL"]
    assert user.slack_user_id == "UBYEMAIL"


def test_an_unverified_email_is_never_looked_up(db_session: Session, make_user) -> None:
    user: User = make_user(email="someone@example.com", email_verified=False)
    slack_client = _RecordingSlackClient()
    slack_client.register_email("someone@example.com", "USOMEONE")

    assert is_full_slack_member(db_session, slack_client, user) is False
    assert slack_client.email_lookups == []
    assert user.slack_user_id is None


def test_no_slack_account_for_the_email_is_not_a_member(db_session: Session, make_user) -> None:
    user: User = make_user(email="nobody@example.com")
    slack_client = _RecordingSlackClient()

    assert is_full_slack_member(db_session, slack_client, user) is False
    assert slack_client.email_lookups == ["nobody@example.com"]
    assert slack_client.user_info_calls == []


def test_an_email_match_already_linked_to_someone_else_is_not_taken(db_session: Session, make_user) -> None:
    make_user(slack_user_id="UTAKEN")
    user: User = make_user(email="dup@example.com")
    slack_client = _RecordingSlackClient()
    slack_client.register_email("dup@example.com", "UTAKEN")

    assert slack_id_for(db_session, slack_client, user) is None
    assert user.slack_user_id is None


def test_the_stored_id_wins_over_an_email_match(db_session: Session, make_user) -> None:
    user: User = make_user(email="both@example.com", slack_user_id="UBYHAND")
    slack_client = _RecordingSlackClient()
    slack_client.register_email("both@example.com", "UBYEMAIL")

    assert slack_id_for(db_session, slack_client, user) == "UBYHAND"
    assert slack_client.email_lookups == []


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
