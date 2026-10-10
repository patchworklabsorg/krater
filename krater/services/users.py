"""Users and authorization: turning what Weave says about a person into a `User` row and an `Actor`.

Weave owns roles. Sign-in reads them from the id_token's `roles` claim; every later authorization
check (`authorize`) asks Weave's directory again, by `weave_sub`. The copies Krater keeps on the user
row (`roles_cached`, `slack_user_id`, `email_verified`) are for display and Slack lookups only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import NotAMember
from krater.weave import WeaveClient, WeaveIdentity, WeaveUser


def _link_slack_id(session: Session, user: User, slack_id: str | None) -> None:
    """Store Weave's `slack_id` on `user`. Weave is the source of truth, so another user row that still
    holds the same id (a stale link) loses it. A missing `slack_id` leaves the stored one alone: it may
    have come from Krater's own Slack email lookup."""
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
    name: str,
    email: str,
    email_verified: bool,
    slack_id: str | None,
    roles: frozenset[str],
) -> None:
    user.display_name = name or email or user.weave_sub
    user.email = email
    user.email_verified = email_verified
    user.roles_cached = sorted(roles)
    _link_slack_id(session, user, slack_id)
    session.flush()


def upsert_user_from_identity(session: Session, identity: WeaveIdentity) -> User:
    """Create or update the `User` row for `identity`, by `weave_sub`.

    Refreshes the display name, email, `email_verified`, `roles_cached` and (if Weave sent one)
    `slack_user_id`. Leaves `last_login_at` alone. Flushes but does not commit.
    """
    user = session.execute(sa.select(User).where(User.weave_sub == identity.sub)).scalar_one_or_none()
    if user is None:
        user = User(weave_sub=identity.sub)
        session.add(user)
    _apply(
        session,
        user,
        name=identity.name,
        email=identity.email,
        email_verified=identity.email_verified,
        slack_id=identity.slack_id,
        roles=identity.roles,
    )
    return user


def refresh_user_from_weave(session: Session, user: User, record: WeaveUser) -> None:
    """Update `user`'s cached fields from a fresh directory record. Flushes but does not commit."""
    _apply(
        session,
        user,
        name=record.name,
        email=record.email,
        email_verified=record.email_verified,
        slack_id=record.slack_id,
        roles=record.roles,
    )


SignInStatus = Literal["ok", "not_a_member"]


@dataclass(frozen=True)
class SignInResult:
    user: User
    status: SignInStatus


def sign_in(session: Session, identity: WeaveIdentity) -> SignInResult:
    """Record a sign-in and decide whether it's allowed. Flushes, never commits.

    The user row is upserted either way, so its cached roles match what Weave just said. Without
    `ganymede:member` in the id_token's roles, the sign-in is refused.
    """
    user = upsert_user_from_identity(session, identity)
    if GROUP_MEMBER not in identity.roles:
        return SignInResult(user=user, status="not_a_member")
    user.last_login_at = datetime.now(UTC)
    session.flush()
    return SignInResult(user=user, status="ok")


def authorize(session: Session, weave_client: WeaveClient, user: User, *, fresh: bool = True) -> Actor:
    """An `Actor` for `user` from a Weave directory lookup by `weave_sub`.

    `fresh` (the default) skips the directory client's short cache, so a role removed in Weave stops the very
    next action. Only a read-only page view may pass `fresh=False`.

    Raises `NotAMember` if Weave doesn't know the user (or won't let them use Krater), lists them as
    inactive, or no longer gives them `ganymede:member`. `WeaveUnavailableError` propagates: callers
    fail closed. Refreshes the user's cached fields from the record (flushed, not committed).
    """
    record = weave_client.get_user(user.weave_sub, fresh=fresh)
    if record is None or not record.active:
        raise NotAMember("Weave no longer lists you as an active Ganymede member")
    refresh_user_from_weave(session, user, record)
    if GROUP_MEMBER not in record.roles:
        raise NotAMember("Weave no longer lists you as an active Ganymede member")
    return Actor(user=user, groups=record.roles, slack_member=record.slack_member)


def cached_actor(user: User) -> Actor:
    """An `Actor` from the roles Weave last reported. Display and navigation only: never use it to
    authorize an action (see `authorize`)."""
    return Actor(user=user, groups=frozenset(user.roles_cached))


__all__ = [
    "SignInResult",
    "authorize",
    "cached_actor",
    "refresh_user_from_weave",
    "sign_in",
    "upsert_user_from_identity",
]
