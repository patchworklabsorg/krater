"""The Slack membership gate (`docs/SPEC.md` "Roles & authentication", "Slack membership gate"), and
the lookup that links a Krater user to their Slack account.

Signing in through Weave doesn't guarantee Slack membership: Weave signup is open, and new users join
Slack as single-channel guests until they accept the code of conduct. Weave's `slack_member` answer
wins when Weave gives one. Otherwise Krater asks Slack itself: the user's Slack account must exist and
not be deleted, `is_restricted` (a multi-channel guest) or `is_ultra_restricted` (a single-channel
guest).
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User
from krater.slack.client import SlackClient

logger = logging.getLogger(__name__)


def slack_id_for(session: Session, slack_client: SlackClient, user: User) -> str | None:
    """The Slack user id Krater uses for `user`, or `None` if there isn't one.

    The stored `User.slack_user_id` wins (Weave's `slack_id`, or an earlier lookup).
    Otherwise Slack `users.lookupByEmail` with the user's email, but only if Weave said that email was
    verified: an unverified address could be anyone's, and a wrong link would let its Slack clicks act
    as this user. A found id is cached onto the user (flushed, not committed), unless another user
    already holds it, which is logged and left for an admin to sort out.
    """
    if user.slack_user_id:
        return user.slack_user_id
    if not user.email_verified or not user.email:
        return None

    slack_id = slack_client.lookup_user_by_email(user.email)
    if not slack_id:
        return None

    holder = session.scalars(sa.select(User).where(User.slack_user_id == slack_id, User.id != user.id)).first()
    if holder is not None:
        logger.warning(
            "Slack account %s matches %s by email but is already linked to user %s; not linking",
            slack_id,
            user.id,
            holder.id,
        )
        return None
    user.slack_user_id = slack_id
    session.flush()
    return slack_id


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
