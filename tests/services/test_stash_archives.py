"""`krater.services.stash_archives`: which submitted links get a `stash_archives` row, and when.

The hook in `projects.submit` and `projects.submit_completion`, the same-transaction guarantee, one row per
project and URL, and what the project page says about each row.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import Project, ReviewDecision, ReviewSource, StashArchive, StashArchiveState
from krater.services import projects, stash_archives
from krater.services.actor import Actor
from tests.factories import new_project

REPO = "https://github.com/someone/rover"
DEMO = "https://rover.example.com/"


def _rows(session: Session, project: Project) -> list[StashArchive]:
    session.flush()
    return list(
        session.scalars(
            sa.select(StashArchive).where(StashArchive.project_id == project.id).order_by(StashArchive.created_at)
        )
    )


def _links(session: Session, project: Project) -> list[tuple[str, str]]:
    return [(row.source, row.url) for row in _rows(session, project)]


def _project(session: Session, member: Actor, **fields) -> Project:
    return new_project(session, member, title="Rover", write_up="A rover.", budget_requested_cents=100_000, **fields)


def _approve(session: Session, reviewer: Actor, project: Project, weave) -> Project:
    projects.record_review(
        session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    session.refresh(project)
    return project


def test_a_draft_has_no_rows(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)

    assert stash_archives.sync_project(db_session, project) == 0
    assert _rows(db_session, project) == []


def test_submitting_a_proposal_writes_a_pending_row_for_the_repository_link(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)

    projects.submit(db_session, member, project=project)

    [row] = _rows(db_session, project)
    assert (row.source, row.url, row.state) == ("repo_url", REPO, StashArchiveState.PENDING)
    assert row.revision_id == project.current_revision_id
    assert (row.archive_id, row.page_url, row.attempts) == (None, None, 0)


def test_a_submission_without_links_writes_nothing(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member)

    projects.submit(db_session, member, project=project)

    assert _rows(db_session, project) == []


def test_the_same_link_is_written_once(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)

    assert stash_archives.sync_project(db_session, project) == 0
    assert _links(db_session, project) == [("repo_url", REPO)]


def test_a_resubmission_with_a_new_link_adds_a_row_and_keeps_the_old_one(
    db_session: Session, member: Actor, reviewer: Actor, weave
) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        source=ReviewSource.WEB,
        reason="Pick another repo.",
        weave_client=weave,
    )
    db_session.refresh(project)

    projects.update_draft(db_session, member, project=project, repo_url="https://github.com/someone/rover2")
    projects.submit(db_session, member, project=project, criteria_read=True)

    assert _links(db_session, project) == [("repo_url", REPO), ("repo_url", "https://github.com/someone/rover2")]


def test_submitting_a_completion_adds_the_demo_link(db_session: Session, member: Actor, reviewer: Actor, weave) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    project = _approve(db_session, reviewer, project, weave)

    projects.start_completion(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, demo_url=DEMO)
    # The draft's demo link waits until the completion is submitted.
    assert _links(db_session, project) == [("repo_url", REPO)]

    projects.submit_completion(db_session, member, project=project)

    assert _links(db_session, project) == [("repo_url", REPO), ("demo_url", DEMO)]
    demo = _rows(db_session, project)[1]
    assert demo.revision_id == project.current_revision_id


def test_a_rolled_back_submission_leaves_no_row(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    project_id = project.id

    projects.submit(db_session, member, project=project)
    assert len(_rows(db_session, project)) == 1
    db_session.rollback()

    remaining = sa.select(sa.func.count()).select_from(StashArchive).where(StashArchive.project_id == project_id)
    assert db_session.scalar(remaining) == 0


def test_withdrawing_skips_the_rows_stash_was_not_asked_about(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    [row] = _rows(db_session, project)

    projects.withdraw(db_session, member, project=project)

    db_session.refresh(row)
    assert row.state is StashArchiveState.SKIPPED
    shown = stash_archives.display(row)
    assert (shown.label, shown.tone) == ("Not archived", "info")


def test_withdrawing_keeps_the_archives_stash_already_has(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    [row] = _rows(db_session, project)
    row.state = StashArchiveState.ARCHIVED
    row.archive_id = "arc_1"
    db_session.flush()

    assert stash_archives.cancel_pending(db_session, project) == 0
    projects.withdraw(db_session, member, project=project)

    db_session.refresh(row)
    assert row.state is StashArchiveState.ARCHIVED


def test_for_project_lists_the_rows_oldest_first(db_session: Session, member: Actor, reviewer: Actor, weave) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    project = _approve(db_session, reviewer, project, weave)
    projects.start_completion(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, demo_url=DEMO)
    projects.submit_completion(db_session, member, project=project)

    rows = stash_archives.for_project(db_session, project)
    assert [row.url for row in rows] == [REPO, DEMO]
    assert rows[0].seq < rows[1].seq


def test_display_reads_each_state(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    [row] = _rows(db_session, project)

    shown = stash_archives.display(row)
    assert (shown.label, shown.tone, shown.page_url, shown.url) == ("Archive pending", "info", None, REPO)

    row.state = StashArchiveState.ARCHIVED
    row.page_url = "https://stash.test/archives/1"
    for status, label, tone in [
        (None, "Archiving", "info"),
        ("queued", "Archiving", "info"),
        ("running", "Archiving", "info"),
        ("done", "Archived", "good"),
        ("partial", "Partly archived", "info"),
        ("failed", "Archive failed", "bad"),
        ("missing", "Archive removed", "bad"),
    ]:
        row.archive_status = status
        shown = stash_archives.display(row)
        assert (shown.label, shown.tone, shown.page_url) == (label, tone, "https://stash.test/archives/1"), status

    row.state = StashArchiveState.FAILED
    assert (stash_archives.display(row).label, stash_archives.display(row).tone) == ("Not archivable", "bad")


def test_display_names_the_problem_when_stash_says_why(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    [row] = _rows(db_session, project)
    row.state = StashArchiveState.ARCHIVED
    row.archive_status = "partial"
    row.problem_hint = "The source is private. Ask the owner to share it, then run the archive again."

    for code, label, tone, fixable in [
        ("not_public", "Link is private", "bad", True),
        ("not_found", "Link is dead", "bad", True),
        ("blocked", "Site refused the archiver", "info", False),
        ("too_large", "Too large to archive", "info", False),
        ("timeout", "Archive timed out", "info", False),
        ("tool_failed", "Partly archived", "info", False),
    ]:
        row.problem_code = code
        shown = stash_archives.display(row)
        assert (shown.label, shown.tone, shown.detail, shown.problem_code) == (label, tone, row.problem_hint, code), (
            code
        )
        assert row.submitter_can_fix is fixable, code

    # A problem on a done archive is stale: the status wins and the hint is not shown.
    row.archive_status = "done"
    shown = stash_archives.display(row)
    assert (shown.label, shown.detail) == ("Archived", None)


def test_problems_for_team_keeps_only_what_the_member_can_fix(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    [row] = _rows(db_session, project)
    row.state = StashArchiveState.ARCHIVED
    row.archive_status = "failed"

    row.problem_code = "blocked"
    assert stash_archives.problems_for_team(db_session, project) == []

    row.problem_code = "not_found"
    row.problem_hint = "Nothing is at that URL."
    [shown] = stash_archives.problems_for_team(db_session, project)
    assert (shown.url, shown.label, shown.detail) == (
        REPO,
        "Link is dead",
        "Nothing is at this link. Send the right URL.",
    )


def test_problems_for_team_ignores_a_link_the_project_no_longer_submits_and_an_archive_still_running(
    db_session: Session, member: Actor
) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    [row] = _rows(db_session, project)
    row.state = StashArchiveState.ARCHIVED
    row.archive_status = "running"  # Stash reports a problem while another capture still runs
    row.problem_code = "not_public"
    row.problem_hint = "Stash's hint"
    assert stash_archives.problems_for_team(db_session, project) == []

    row.archive_status = "failed"
    [shown] = stash_archives.problems_for_team(db_session, project)
    assert shown.detail == "This link needs a sign-in or a share. Make it public, or send a public URL."

    # The member sends another link: the old one is no longer part of the submission.
    project.repo_url = "https://github.com/someone/rover2"
    db_session.flush()
    assert stash_archives.problems_for_team(db_session, project) == []


def test_a_resubmission_with_the_same_link_asks_stash_again_after_a_problem_the_member_fixed(
    db_session: Session, member: Actor, reviewer: Actor, weave
) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    project = _approve(db_session, reviewer, project, weave)
    [row] = _rows(db_session, project)
    first_revision_id = row.revision_id
    row.state = StashArchiveState.ARCHIVED
    row.archive_id = "arc_1"
    row.page_url = "https://stash.test/archives/1"
    row.archive_status = "failed"
    row.problem_code = "not_public"
    row.problem_hint = "private"
    row.attempts = 3
    row.last_status = 201
    db_session.flush()

    # An amendment with the same link, after the member made the repository public.
    projects.start_amendment(db_session, member, project=project)
    projects.submit(db_session, member, project=project, criteria_read=True)

    [row] = _rows(db_session, project)
    assert row.state is StashArchiveState.PENDING
    assert row.revision_id == project.current_revision_id != first_revision_id
    assert (row.archive_id, row.archive_status, row.problem_code, row.attempts, row.last_status) == (
        None,
        None,
        None,
        0,
        None,
    )
    assert stash_archives.problems_for_team(db_session, project) == []

    # A second submit of the same revision changes nothing more.
    assert stash_archives.sync_project(db_session, project) == 0


def test_a_resubmission_leaves_an_archive_alone_when_the_problem_is_not_the_members(
    db_session: Session, member: Actor, reviewer: Actor, weave
) -> None:
    project = _project(db_session, member, repo_url=REPO)
    projects.submit(db_session, member, project=project)
    project = _approve(db_session, reviewer, project, weave)
    [row] = _rows(db_session, project)
    row.state = StashArchiveState.ARCHIVED
    row.archive_id = "arc_1"
    row.archive_status = "partial"
    row.problem_code = "blocked"
    db_session.flush()

    projects.start_amendment(db_session, member, project=project)
    projects.submit(db_session, member, project=project, criteria_read=True)

    [row] = _rows(db_session, project)
    assert (row.state, row.archive_id, row.problem_code) == (StashArchiveState.ARCHIVED, "arc_1", "blocked")
