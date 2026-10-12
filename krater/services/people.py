"""Members' names and emails, read from Weave when Krater shows or uses them.

Weave stores member data; Krater keeps only the Patchwork ID (`User.weave_sub`) and asks the directory
when a page needs a name (docs/weave-integration.md, patchworklabsorg/krater#7). The directory client
caches answers for a short time, so a page listing a few people costs a few lookups at most.

A name is for display, so a lookup that fails (Weave down, or the account gone) falls back to the
Patchwork ID instead of failing the page. Nothing here authorizes anything: that is
`krater.services.users.authorize`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.actor import GROUP_MEMBER
from krater.weave import WeaveClient, WeaveUnavailableError, WeaveUser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Person:
    """What Weave says about one member right now. `email` is `None` when Weave didn't answer."""

    sub: str
    name: str
    email: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.sub


def lookup(weave_client: WeaveClient, sub: str) -> Person:
    """The member with Patchwork ID `sub`, or just the ID when Weave can't say."""
    try:
        record = weave_client.get_user(sub)
    except WeaveUnavailableError:
        logger.warning("Weave unavailable; showing %s by Patchwork ID", sub)
        record = None
    if record is None:
        return Person(sub=sub, name=sub)
    return Person(sub=sub, name=record.name or sub, email=record.email or None)


def names(weave_client: WeaveClient, users: Iterable[User]) -> dict[uuid.UUID, str]:
    """`User.id` -> display name, for every user in `users`."""
    return {user.id: lookup(weave_client, user.weave_sub).label for user in users}


def find_member(weave_client: WeaveClient, query: str) -> WeaveUser | None:
    """The active Ganymede member whose Patchwork ID or email is `query`, or `None`.

    A Patchwork ID is looked up directly. Weave's directory has no search by email, so an email is
    matched against the list of members; only members are ever returned, because only they can use
    Krater. Raises `WeaveUnavailableError` if Weave can't answer: callers show an error rather than
    "not found".
    """
    query = query.strip()
    if not query:
        return None
    if "@" not in query:
        record = weave_client.get_user(query.upper(), fresh=True)
        if record is not None and record.active and GROUP_MEMBER in record.roles:
            return record
        return None
    wanted = query.casefold()
    for record in weave_client.list_users_with_role(GROUP_MEMBER):
        if record.active and record.email and record.email.casefold() == wanted:
            return record
    return None


def user_for(session: Session, record: WeaveUser) -> User:
    """The Krater user row for a Weave member, created (holding only the Patchwork ID) if they have never
    signed in to Krater. Flushes, doesn't commit."""
    user = session.scalar(sa.select(User).where(User.weave_sub == record.sub))
    if user is None:
        user = User(weave_sub=record.sub, roles_cached=sorted(record.roles))
        session.add(user)
        session.flush()
    return user


__all__ = ["Person", "find_member", "lookup", "names", "user_for"]
