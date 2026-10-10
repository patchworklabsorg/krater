"""Shared fixtures for `tests/services`: quick user/actor factories, and a stub Weave that knows them."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.weave import RoleMapping, StubWeaveClient

_counter = itertools.count()


@pytest.fixture
def make_user(db_session: Session):
    """Factory: create and flush a `User` with a unique `weave_sub`/email."""

    def _make_user(
        *,
        display_name: str = "Test User",
        email: str | None = None,
        email_verified: bool = True,
        slack_user_id: str | None = None,
    ) -> User:
        n = next(_counter)
        user = User(
            weave_sub=f"PWLTEST{n:06d}",
            display_name=display_name,
            email=email or f"user{n}@example.com",
            email_verified=email_verified,
            slack_user_id=slack_user_id,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make_user


@pytest.fixture
def weave(tmp_path) -> StubWeaveClient:
    """An empty stub Weave for this test. `make_actor` registers every actor in it, with their roles as
    Weave role keys, so anything that asks Weave (Slack clicks, reviewer invites) sees the same roles."""
    empty = tmp_path / "no_users.json"
    empty.write_text("[]")
    return StubWeaveClient(empty)


def register_in_weave(weave: StubWeaveClient, user: User, groups: frozenset[str], **kwargs) -> None:
    """Add `user` to the stub Weave holding the Krater roles `groups`. Roles with no Weave key (such as
    a reviewer tier) are left out."""
    mapping = RoleMapping.default()
    weave.put_user(
        user.weave_sub,
        name=user.display_name,
        email=user.email,
        email_verified=user.email_verified,
        roles=[mapping.weave_role_key(role) for role in sorted(groups) if role in mapping.role_keys],
        **kwargs,
    )


@pytest.fixture
def make_actor(db_session: Session, make_user, weave: StubWeaveClient):
    """Factory: build an `Actor` with the given groups, backed by a fresh (or supplied) `User`. The
    groups are also cached on the user and registered in the `weave` stub."""

    def _make_actor(*, groups: frozenset[str] = frozenset(), user: User | None = None, **user_kwargs) -> Actor:
        user = user or make_user(**user_kwargs)
        user.roles_cached = sorted(groups)
        db_session.flush()
        register_in_weave(weave, user, groups)
        return Actor(user=user, groups=groups)

    return _make_actor


@pytest.fixture
def member(make_actor) -> Actor:
    """A plain Ganymede member: can submit, but not review or admin-override."""
    return make_actor(groups=frozenset({GROUP_MEMBER}))


@pytest.fixture
def reviewer(make_actor) -> Actor:
    """A Ganymede reviewer (and member)."""
    return make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))


@pytest.fixture
def admin(make_actor) -> Actor:
    """A Ganymede admin (and member)."""
    return make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
