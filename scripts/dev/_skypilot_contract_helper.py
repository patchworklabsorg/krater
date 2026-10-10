#!/usr/bin/env python3
"""Small DB-mutation helpers for `scripts/dev/skypilot_contract.sh`'s launch-gate demo.

Not a general-purpose tool -- just the handful of one-off project/budget mutations the shell script
needs against a running Krater's own database, done through `krater.services` (never raw SQL) so they
go through the same rules a real approval/withdrawal would. `KRATER_DATABASE_URL` must already point at
a migrated database (the shell script runs `alembic upgrade head` before calling this).

Usage:
    _skypilot_contract_helper.py create-project <budget_cents>
    _skypilot_contract_helper.py set-overbudget <project_id>
    _skypilot_contract_helper.py clear-spend <project_id>
    _skypilot_contract_helper.py withdraw-project <project_id>
    _skypilot_contract_helper.py workspace <project_id>   (prints its SkyPilot workspace; empty once torn down)
"""

from __future__ import annotations

import json
import sys
import uuid

from krater.db import get_sessionmaker
from krater.models import Project, ReviewDecision, ReviewSource, SpendSnapshot, SpendSource, User
from krater.services import projects
from krater.services.actor import Actor
from krater.services.skypilot_sync import workspace_name_for

# Same stub users `tests/conftest.py` and the rest of the suite use (krater/weave/stub_users.json).
SUBMITTER_EMAIL = "mia@example.com"
REVIEWER_EMAIL = "rae@example.com"

# Their stub Weave subs. The reconciler only lets in people the stub directory lists as members.
STUB_SUBS = {SUBMITTER_EMAIL: "PWLMEMBERONE", REVIEWER_EMAIL: "PWLREVIEWERONE"}


def _actor(session, *, email: str, name: str, groups: frozenset[str]) -> Actor:
    user = session.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(weave_sub=STUB_SUBS[email], display_name=name, email=email)
        session.add(user)
        session.flush()
    return Actor(user=user, groups=groups)


def create_project(budget_cents: int) -> None:
    session = get_sessionmaker()()
    try:
        member = _actor(session, email=SUBMITTER_EMAIL, name="Mia Member", groups=frozenset({"ganymede:member"}))
        reviewer = _actor(
            session,
            email=REVIEWER_EMAIL,
            name="Rae Reviewer",
            groups=frozenset({"ganymede:member", "ganymede:reviewer"}),
        )
        project = projects.create_project(
            session, member, title="SkyPilot Contract Check", write_up="Automated contract check."
        )
        projects.update_draft(session, member, project=project, budget_requested_cents=budget_cents)
        project = projects.submit(session, member, project=project)
        projects.record_review(
            session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
        session.commit()
        session.refresh(project)
        print(json.dumps({"project_id": str(project.id), "workspace": workspace_name_for(project.id)}))
    finally:
        session.close()


def set_overbudget(project_id: str) -> None:
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            raise SystemExit(f"no such project: {project_id}")
        # A ceiling-matching spend, regardless of what the ceiling actually is -- simplest way to
        # trip `budget.remaining_cents(...) <= 0` without needing to know the exact figure here.
        # `SpendSource` has only one member today (real spend always comes from `cost_report`), but a
        # manual test snapshot is exactly what it would report anyway once the workspace existed.
        ceiling_cents = sum(entry.amount_cents for entry in project.budget_entries)
        session.add(
            SpendSnapshot(
                project_id=project.id, estimated_spend_cents=ceiling_cents, source=SpendSource.SKYPILOT_COST_REPORT
            )
        )
        session.commit()
    finally:
        session.close()


def clear_spend(project_id: str) -> None:
    session = get_sessionmaker()()
    try:
        pid = uuid.UUID(project_id)
        session.query(SpendSnapshot).filter_by(project_id=pid).delete()
        session.commit()
    finally:
        session.close()


def withdraw_project(project_id: str) -> None:
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            raise SystemExit(f"no such project: {project_id}")
        actor = Actor(user=project.submitter, groups=frozenset({"ganymede:member"}))
        projects.withdraw(session, actor, project=project)
        session.commit()
    finally:
        session.close()


def print_workspace(project_id: str) -> None:
    session = get_sessionmaker()()
    try:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            raise SystemExit(f"no such project: {project_id}")
        print(project.skypilot_workspace or "")
    finally:
        session.close()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    command, args = sys.argv[1], sys.argv[2:]
    if command == "create-project":
        create_project(int(args[0]))
    elif command == "set-overbudget":
        set_overbudget(args[0])
    elif command == "clear-spend":
        clear_spend(args[0])
    elif command == "withdraw-project":
        withdraw_project(args[0])
    elif command == "workspace":
        print_workspace(args[0])
    else:
        raise SystemExit(f"unknown command: {command}\n\n{__doc__}")


if __name__ == "__main__":
    main()
