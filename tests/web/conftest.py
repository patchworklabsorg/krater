"""Shared fixtures for tests/web: quick, direct-to-service project setup for a signed-in stub user.

HTTP-level tests exercise routing, auth wiring, CSRF and error-mapping -- the service layer's own
rules already have thorough unit tests in tests/services. So these fixtures skip the HTTP round trip
for *setup* (e.g. getting a project into `pending_review`) and call the service directly, using an
`Actor` built from the signed-in `User`'s roles as stub Weave reported them at sign-in.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from krater.models import Project, ReviewDecision, ReviewSource, User
from krater.services import projects as project_service
from krater.services import users as user_service
from krater.services.actor import GROUP_MEMBER, Actor
from krater.weave import RoleMapping, StubWeaveClient

#: A real PNG magic-byte header, for tests that need `confirm_screenshot`'s signature check to pass.
PNG_SIGNATURE = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)) + b"\x00" * 8


def actor_for(user: User) -> Actor:
    """An `Actor` for `user`, with the roles Weave reported at their sign-in."""
    return user_service.cached_actor(user)


@pytest.fixture
def create_project(db_session: Session) -> Callable[..., Project]:
    """Factory: create a draft project for `user`, via the service (bypassing the HTTP form)."""

    def _create(user: User, **kwargs) -> Project:
        kwargs.setdefault("title", "Test Project")
        kwargs.setdefault("write_up", "A write-up.")
        kwargs.setdefault("budget_requested_cents", 10_000)
        return project_service.create_project(db_session, actor_for(user), **kwargs)

    return _create


@pytest.fixture
def submitted_project(db_session: Session, create_project: Callable[..., Project]) -> Callable[..., Project]:
    """Factory: create and submit a project for `user`, landing it in `pending_review`."""

    def _submit(user: User, **kwargs) -> Project:
        project = create_project(user, **kwargs)
        return project_service.submit(db_session, actor_for(user), project=project)

    return _submit


@pytest.fixture
def approved_project(db_session: Session, submitted_project: Callable[..., Project]) -> Callable[..., Project]:
    """Factory: create, submit and get `reviewer` to approve a project for `user`."""

    def _approve(user: User, reviewer: User, **kwargs) -> Project:
        project = submitted_project(user, **kwargs)
        project_service.record_review(
            db_session,
            actor_for(reviewer),
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
        db_session.refresh(project)
        return project

    return _approve


@pytest.fixture
def revoke_membership(weave_stub: StubWeaveClient) -> Callable[[User], None]:
    """Take Krater's `member` role away from an already-signed-in `user` in the stub Weave, so
    `fresh_actor` sees the change on their *next* request (sign-in itself already refuses non-members)."""

    def _revoke(user: User) -> None:
        record = weave_stub.get_user(user.weave_sub)
        assert record is not None
        mapping = RoleMapping.default()
        weave_stub.set_roles(
            user.weave_sub, [mapping.weave_role_key(role) for role in record.roles if role != GROUP_MEMBER]
        )

    return _revoke
