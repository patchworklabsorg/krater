"""Invalid transitions, input validation, and the read-model queries."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from krater.models import ProjectStatus, ReviewDecision, ReviewSource, RevisionKind
from krater.services import projects
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed


def test_create_project_requires_membership(db_session: Session, make_actor) -> None:
    non_member = make_actor(groups=frozenset())

    with pytest.raises(NotAllowed):
        projects.create_project(db_session, non_member, title="x", write_up="y", budget_requested_cents=100)


def test_update_draft_requires_submitter(db_session: Session, member: Actor, make_actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    someone_else = make_actor(groups=frozenset({"ganymede:member"}))

    with pytest.raises(NotAllowed):
        projects.update_draft(db_session, someone_else, project=project, title="hijacked")


def test_update_draft_fails_when_no_draft_exists(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    project = projects.submit(db_session, member, project=project)

    with pytest.raises(InvalidState):
        projects.update_draft(db_session, member, project=project, title="too late")


def test_submit_validates_required_fields(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member)  # blank title/write-up, budget 0

    with pytest.raises(ValidationFailed) as excinfo:
        projects.submit(db_session, member, project=project)

    assert set(excinfo.value.errors) == {"title", "write_up", "budget_requested_cents"}


def test_submit_fails_without_a_draft(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    project = projects.submit(db_session, member, project=project)

    with pytest.raises(InvalidState):
        projects.submit(db_session, member, project=project)


def test_submit_on_a_completion_draft_directs_to_submit_completion(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    project = projects.submit(db_session, member, project=project)
    projects.admin_decide(
        db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="ok"
    )
    db_session.refresh(project)
    projects.start_completion(db_session, member, project=project)

    with pytest.raises(InvalidState):
        projects.submit(db_session, member, project=project)


def test_start_amendment_requires_an_approved_project(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)

    with pytest.raises(InvalidState):
        projects.start_amendment(db_session, member, project=project)


def test_start_completion_requires_an_approved_project(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)

    with pytest.raises(InvalidState):
        projects.start_completion(db_session, member, project=project)


def test_start_amendment_fails_with_a_draft_already_in_progress(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    project = projects.submit(db_session, member, project=project)
    projects.admin_decide(
        db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="ok"
    )
    db_session.refresh(project)
    projects.start_amendment(db_session, member, project=project)

    with pytest.raises(InvalidState):
        projects.start_amendment(db_session, member, project=project)


def test_submit_completion_requires_a_completion_draft(db_session: Session, member: Actor, admin: Actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    project = projects.submit(db_session, member, project=project)
    projects.admin_decide(
        db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="ok"
    )
    db_session.refresh(project)

    with pytest.raises(InvalidState):
        projects.submit_completion(db_session, member, project=project)


def test_get_project_not_found_raises(db_session: Session) -> None:
    with pytest.raises(NotFound):
        projects.get_project(db_session, project_id=uuid.uuid4())


def test_get_project_returns_the_project(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)

    found = projects.get_project(db_session, project_id=project.id)

    assert found.id == project.id


def test_list_projects_for_user(db_session: Session, member: Actor, make_actor) -> None:
    other = make_actor(groups=frozenset({"ganymede:member"}))
    mine = projects.create_project(db_session, member, title="mine", write_up="y", budget_requested_cents=100)
    projects.create_project(db_session, other, title="theirs", write_up="y", budget_requested_cents=100)

    result = projects.list_projects_for_user(db_session, user_id=member.user.id)

    assert [p.id for p in result] == [mine.id]


def test_review_queue_requires_reviewer(db_session: Session, member: Actor) -> None:
    with pytest.raises(NotAllowed):
        projects.review_queue(db_session, member)


def test_review_queue_excludes_own_projects_credited_builders_and_already_reviewed(
    db_session: Session, member: Actor, reviewer: Actor, make_actor
) -> None:
    other_member = make_actor(groups=frozenset({"ganymede:member"}))

    # A submission by someone else: should show up.
    visible = projects.create_project(
        db_session, other_member, title="visible", write_up="y", budget_requested_cents=100
    )
    visible = projects.submit(db_session, other_member, project=visible)

    # reviewer's own submission: excluded.
    own = projects.create_project(db_session, reviewer, title="own", write_up="y", budget_requested_cents=100)
    projects.submit(db_session, reviewer, project=own)

    # A submission crediting the reviewer as a builder: excluded.
    credited = projects.create_project(
        db_session, other_member, title="credited", write_up="y", budget_requested_cents=100
    )
    credited.current_revision.credited_builder_ids = [reviewer.user.id]
    db_session.flush()
    credited = projects.submit(db_session, other_member, project=credited)

    queue_before = {r.id for r in projects.review_queue(db_session, reviewer)}
    assert visible.current_revision.id in queue_before
    assert own.current_revision.id not in queue_before
    assert credited.current_revision.id not in queue_before

    # Once reviewed, it drops out of the queue too.
    projects.record_review(
        db_session,
        reviewer,
        revision=visible.current_revision,
        decision=ReviewDecision.REJECT,
        reason="not ready",
        source=ReviewSource.WEB,
    )
    queue_after = {r.id for r in projects.review_queue(db_session, reviewer)}
    assert visible.current_revision.id not in queue_after  # rejected -> no longer pending anyway


def test_project_summary_reports_policy_explanation_while_pending(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=1_000)
    project = projects.submit(db_session, member, project=project)

    summary = projects.project_summary(db_session, project=project)
    assert summary.status is ProjectStatus.PENDING_REVIEW
    assert summary.policy_explanation == "Needs 1 more approval."
    assert summary.ceiling_cents == 0
    assert summary.remaining_cents == 0

    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    summary = projects.project_summary(db_session, project=project)
    assert summary.status is ProjectStatus.APPROVED
    assert summary.policy_explanation is None
    assert summary.ceiling_cents == 1_000
    assert summary.current_revision.kind is RevisionKind.PROPOSAL
    assert summary.approved_revision is not None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>x</script>",
        "/relative/path",
        "ftp://example.com/x",
        "https://",
        "https://example.com/a b",
    ],
)
def test_links_must_be_http_urls(db_session: Session, member: Actor, url: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100, repo_url=url)
    assert "repo_url" in exc.value.errors

    project = projects.create_project(db_session, member, title="x", write_up="y", budget_requested_cents=100)
    with pytest.raises(ValidationFailed) as exc:
        projects.update_draft(db_session, member, project=project, demo_url=url)
    assert "demo_url" in exc.value.errors


def test_links_accept_http_urls_and_blank_clears(db_session: Session, member: Actor) -> None:
    project = projects.create_project(
        db_session, member, title="x", write_up="y", budget_requested_cents=100, repo_url="  https://github.com/o/r  "
    )
    assert project.repo_url == "https://github.com/o/r"

    projects.update_draft(db_session, member, project=project, repo_url="")
    assert project.repo_url is None
