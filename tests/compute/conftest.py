"""Fixtures for the Krater Compute tests: a FakeVast, a client wired to it, and an approved project."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.models import ReviewDecision
from krater.services import projects
from krater.services.actor import Actor
from krater.vast import FakeVast, VastClient
from tests.factories import new_project
from tests.services.conftest import admin, make_actor, make_user, member, weave  # noqa: F401


@pytest.fixture
def fake() -> FakeVast:
    return FakeVast()


@pytest.fixture
def client(fake: FakeVast) -> VastClient:
    return fake.client()


@pytest.fixture
def project(db_session: Session, member: Actor, admin: Actor, weave):  # noqa: F811
    project = new_project(db_session, member, title="Compute Test", write_up="w", budget_requested_cents=10_000)
    projects.submit(db_session, member, project=project)
    projects.admin_decide(
        db_session,
        admin,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        reason="ok",
        weave_client=weave,
    )
    db_session.refresh(project)
    return project
