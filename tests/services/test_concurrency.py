"""A real concurrency test for the two-reviewers-approve-at-once race (finding: concurrent approvals
double-apply budget). It uses two threads and two independent `Session`s against the real test
database, since it needs both threads to see each other's *committed* work -- something the rest of
the suite's SAVEPOINT-per-test `db_session` fixture (see `tests/conftest.py`) can't do, as a nested
SAVEPOINT's writes aren't visible to a second connection until the outer transaction (which the fixture
never commits) does. So this test manages its own committed rows against the shared `engine` fixture,
and cleans them up itself in a `finally` block. Adapted from the reviewer's `race.py` reproduction.
"""

from __future__ import annotations

import threading
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from krater.models import (
    BudgetEntry,
    Project,
    ProjectRevision,
    ProjectStatus,
    QuiltOutbox,
    Review,
    ReviewDecision,
    ReviewSource,
    User,
)
from krater.services import projects
from krater.services.actor import Actor
from krater.services.errors import DomainError

_MEMBER = frozenset({"ganymede:member"})
_REVIEWER = frozenset({"ganymede:member", "ganymede:reviewer"})


def _make_user(session: Session, tag: str) -> User:
    unique = uuid.uuid4().hex
    user = User(weave_sub=f"RACE-{tag}-{unique}", display_name=f"Race {tag}", email=f"race-{tag}-{unique}@example.com")
    session.add(user)
    session.flush()
    return user


def _cleanup(engine: Engine, *, project_id: uuid.UUID, user_ids: list[uuid.UUID]) -> None:
    with Session(engine) as session:
        session.execute(
            sa.update(Project)
            .where(Project.id == project_id)
            .values(current_revision_id=None, approved_revision_id=None)
        )
        session.execute(
            sa.delete(Review).where(
                Review.revision_id.in_(sa.select(ProjectRevision.id).where(ProjectRevision.project_id == project_id))
            )
        )
        session.execute(sa.delete(BudgetEntry).where(BudgetEntry.project_id == project_id))
        session.execute(sa.delete(QuiltOutbox).where(QuiltOutbox.external_id == str(project_id)))
        session.execute(sa.delete(ProjectRevision).where(ProjectRevision.project_id == project_id))
        session.execute(sa.delete(Project).where(Project.id == project_id))
        session.execute(sa.delete(User).where(User.id.in_(user_ids)))
        session.commit()


def test_two_concurrent_approvals_do_not_double_apply_the_budget(engine: Engine) -> None:
    """Two reviewers approve the same revision at (as close to) the same instant. Without a lock on the
    project row, both threads can each read the revision as still-`pending`, each independently see the
    (default, 1-approval) policy as satisfied by their own review, and each run `_apply_approve` --
    writing two `initial_approval` `BudgetEntry` rows and granting the budget twice. With the lock, the
    second thread blocks until the first commits, then re-reads and finds the revision already decided.
    """
    with Session(engine) as setup:
        submitter = _make_user(setup, "sub")
        reviewer_a = _make_user(setup, "r1")
        reviewer_b = _make_user(setup, "r2")
        project = projects.create_project(
            setup, Actor(submitter, _MEMBER), title="Race", write_up="w", budget_requested_cents=10_000
        )
        projects.submit(setup, Actor(submitter, _MEMBER), project=project)
        setup.commit()
        project_id = project.id
        revision_id = project.current_revision_id
        user_ids = [submitter.id, reviewer_a.id, reviewer_b.id]

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(reviewer_id: uuid.UUID) -> None:
        with Session(engine) as session:
            revision = session.get(ProjectRevision, revision_id)
            reviewer = session.get(User, reviewer_id)
            actor = Actor(reviewer, _REVIEWER)
            barrier.wait()  # both threads start their `record_review` call at the same time
            try:
                projects.record_review(
                    session, actor, revision=revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
                )
                session.commit()
                with lock:
                    outcomes.append("approved")
            except DomainError:
                session.rollback()
                with lock:
                    outcomes.append("rejected")

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in (reviewer_a.id, reviewer_b.id)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one of the two approvals must have gone through; the other must have lost the race
        # against the lock and been rejected by the (re-validated, post-lock) state check.
        assert sorted(outcomes) == ["approved", "rejected"]

        with Session(engine) as check:
            reloaded = check.get(Project, project_id)
            assert reloaded.status is ProjectStatus.APPROVED

            entries = check.scalars(sa.select(BudgetEntry).where(BudgetEntry.project_id == project_id)).all()
            initial_approvals = [e for e in entries if e.kind.value == "initial_approval"]
            assert len(initial_approvals) == 1, [e.amount_cents for e in initial_approvals]
            assert initial_approvals[0].amount_cents == 10_000

            reviews = check.scalars(sa.select(Review).where(Review.revision_id == revision_id)).all()
            assert len(reviews) == 1
    finally:
        _cleanup(engine, project_id=project_id, user_ids=user_ids)
