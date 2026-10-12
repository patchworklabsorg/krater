"""The Slack membership gate (`docs/SPEC.md` "Roles & authentication", "Slack membership gate"), and
the lookup that links a Krater user to their Slack account.

Signing in through Weave doesn't guarantee Slack membership: Weave signup is open, and new users join
Slack as single-channel guests until they accept the code of conduct. Weave's `slack_member` answer
wins when Weave gives one. Otherwise Krater asks Slack about the `slack_id` Weave gave: the account must
exist and not be deleted, `is_restricted` (a multi-channel guest) or `is_ultra_restricted` (a single-channel
guest).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import User
from krater.slack.client import SlackClient


def slack_id_for(session: Session, slack_client: SlackClient, user: User) -> str | None:
    """The Slack user id Krater uses for `user`: Weave's `slack_id`, stored on the user at sign-in and at
    every directory lookup. `None` when Weave hasn't given one. Krater no longer looks Slack accounts up
    by email, since it keeps no emails (docs/weave-integration.md)."""
    return user.slack_user_id


def is_full_slack_member(
    session: Session, slack_client: SlackClient, user: User, *, weave_slack_member: bool | None = None
) -> bool:
    """Whether `user` is a full (non-guest, active) member of the Patchwork Labs Slack.

    `weave_slack_member` is Weave's `slack_member` answer from a fresh directory lookup
    (`Actor.slack_member`). When Weave gave one, it decides. Otherwise this resolves the Slack account
    with `slack_id_for` and asks `users.info`: the account must exist and not be deleted,
    `is_restricted` or `is_ultra_restricted`. No Slack account means not a member. Slack errors
    propagate rather than being read as either answer.
    """
    if weave_slack_member is not None:
        return weave_slack_member
    slack_id = slack_id_for(session, slack_client, user)
    if not slack_id:
        return False
    info = slack_client.get_user_info(slack_id)
    if info is None:
        return False
    return not (info.deleted or info.is_restricted or info.is_ultra_restricted)


__all__ = ["is_full_slack_member", "slack_id_for"]
