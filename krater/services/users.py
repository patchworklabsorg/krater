"""Users and authorization: turning what Weave says about a person into a `User` row and an `Actor`.

Weave decides who may use Krater (the app's access policy) and owns roles. Every Patchwork member Weave
signs in is a Ganymede member. Sign-in reads the roles from the id_token's `roles` claim; every later
authorization check (`authorize`) asks Weave's directory again, by `weave_sub`. The user row keeps no name or email
(docs/weave-integration.md); `roles_cached` and `slack_user_id` are for display and Slack clicks only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.actor import Actor
from krater.services.errors import NotAMember
from krater.weave import WeaveClient, WeaveIdentity, WeaveUser


def _link_slack_id(session: Session, user: User, slack_id: str | None) -> None:
    """Store Weave's `slack_id` on `user`. Weave is the source of truth, so another user row that still
    holds the same id (a stale link) loses it. A missing `slack_id` leaves the stored one alone."""
    if not slack_id or user.slack_user_id == slack_id:
        return
    session.execute(
        sa.update(User).where(User.slack_user_id == slack_id, User.id != user.id).values(slack_user_id=None)
    )
    user.slack_user_id = slack_id


def _apply(
    session: Session,
    user: User,
    *,
    slack_id: str | None,
    roles: frozenset[str],
) -> None:
    user.roles_cached = sorted(roles)
    _link_slack_id(session, user, slack_id)
    session.flush()


def upsert_user_from_identity(session: Session, identity: WeaveIdentity) -> User:
    """Create or update the `User` row for `identity`, by `weave_sub`.

    Refreshes `roles_cached` and (if Weave sent one) `slack_user_id`. Leaves `last_login_at` alone. Flushes but
    does not commit.
    """
    user = session.execute(sa.select(User).where(User.weave_sub == identity.sub)).scalar_one_or_none()
    if user is None:
        user = User(weave_sub=identity.sub)
        session.add(user)
    _apply(
        session,
        user,
        slack_id=identity.slack_id,
        roles=identity.roles,
    )
    return user


def refresh_user_from_weave(session: Session, user: User, record: WeaveUser) -> None:
    """Update `user`'s cached roles and Slack id from a fresh directory record. Flushes but does not commit."""
    _apply(
        session,
        user,
        slack_id=record.slack_id,
        roles=record.roles,
    )


def sign_in(session: Session, identity: WeaveIdentity) -> User:
    """Record a sign-in. Flushes, never commits.

    Weave only signs in people its access policy lets use Krater, so every sign-in is allowed. The user row is
    upserted, so its cached roles match what Weave just said.
    """
    user = upsert_user_from_identity(session, identity)
    user.last_login_at = datetime.now(UTC)
    session.flush()
    return user


def authorize(session: Session, weave_client: WeaveClient, user: User, *, fresh: bool = True) -> Actor:
    """An `Actor` for `user` from a Weave directory lookup by `weave_sub`.

    `fresh` (the default) skips the directory client's short cache, so a role removed in Weave stops the very
    next action. Only a read-only page view may pass `fresh=False`.

    Raises `NotAMember` if Weave doesn't know the user (or won't let them use Krater) or lists them as
    inactive. `WeaveUnavailableError` propagates: callers fail closed. Refreshes the user's cached roles
    from the record (flushed, not committed). The actor carries the record's name and email for this
    request only.
    """
    record = weave_client.get_user(user.weave_sub, fresh=fresh)
    if record is None or not record.active:
        raise NotAMember("Weave no longer lists you as an active Ganymede member")
    refresh_user_from_weave(session, user, record)
    return Actor(
        user=user,
        groups=record.roles,
        slack_member=record.slack_member,
        slack_required=record.slack_required,
        code_of_conduct_required=record.code_of_conduct_required,
        name=record.name,
        email=record.email,
    )


def cached_actor(user: User) -> Actor:
    """An `Actor` from the roles Weave last reported. Display and navigation only: never use it to
    authorize an action (see `authorize`)."""
    return Actor(user=user, groups=frozenset(user.roles_cached))


__all__ = [
    "authorize",
    "cached_actor",
    "refresh_user_from_weave",
    "sign_in",
    "upsert_user_from_identity",
]
