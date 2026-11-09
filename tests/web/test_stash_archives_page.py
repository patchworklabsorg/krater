"""The archive links on the project page: reviewers and admins see Stash's copy of each submitted link, the
team doesn't, and a link appears only once Stash has answered."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import StashArchiveState
from krater.services import stash_archives
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB

REPO = "https://github.com/someone/rover"
PAGE = "https://stash.test/archives/4k8xJm2pN9qW"


def _archived(db_session: Session, project) -> None:
    [row] = stash_archives.for_project(db_session, project)
    row.state = StashArchiveState.ARCHIVED
    row.archive_id = "arc_4k8xJm2pN9qW"
    row.page_url = PAGE
    row.archive_status = "done"
    db_session.flush()


def test_a_reviewer_sees_the_archive_link_and_its_status(
    client: TestClient, db_session: Session, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover", repo_url=REPO)
    _archived(db_session, project)

    login_as(REVIEWER_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Archived copies" in response.text
    assert f'<a href="{PAGE}">{REPO}</a>' in response.text
    assert "Archived</span>" in response.text


def test_an_admin_sees_a_pending_archive_without_a_link(
    client: TestClient, db_session: Session, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover", repo_url=REPO)

    login_as(ADMIN_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Archive pending" in response.text
    assert "stash.test" not in response.text


def test_the_team_does_not_see_the_archives(
    client: TestClient, db_session: Session, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover", repo_url=REPO)
    _archived(db_session, project)

    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Archived copies" not in response.text
    assert PAGE not in response.text


def _failed_with(db_session: Session, project, code: str, hint: str, status: str = "partial") -> None:
    _archived(db_session, project)
    [row] = stash_archives.for_project(db_session, project)
    row.archive_status = status
    row.problem_code = code
    row.problem_hint = hint
    row.problem_message = f"{code}: the detail for whoever debugs"
    db_session.flush()


PRIVATE_HINT = "The source is private. Ask the owner to share it with anyone with the link, then run the archive again."


def test_the_team_is_told_about_a_private_link_and_nothing_else(
    client: TestClient, db_session: Session, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover", repo_url=REPO)
    _failed_with(db_session, project, "not_public", PRIVATE_HINT)

    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "A link you submitted cannot be archived" in response.text
    assert "Link is private" in response.text
    assert PRIVATE_HINT in response.text
    assert REPO in response.text
    # Not the reviewers' section, not Stash's page, not the debugging detail.
    assert "Archived copies" not in response.text
    assert PAGE not in response.text
    assert "whoever debugs" not in response.text


def test_the_team_is_not_told_about_a_problem_only_stash_can_fix(
    client: TestClient, db_session: Session, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover", repo_url=REPO)
    _failed_with(db_session, project, "blocked", "The site refused the archiver. Run the archive again later.")

    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "cannot be archived" not in response.text


def test_a_reviewer_sees_why_an_archive_failed(
    client: TestClient, db_session: Session, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover", repo_url=REPO)
    _failed_with(
        db_session,
        project,
        "not_found",
        "Nothing is at that URL. Ask for the right link, then run the archive again.",
        status="failed",
    )

    login_as(REVIEWER_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Link is dead</span>" in response.text
    assert "Nothing is at that URL" in response.text
    assert f'<a href="{PAGE}">{REPO}</a>' in response.text


def test_a_project_without_links_shows_no_archive_section(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover")

    login_as(REVIEWER_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Archived copies" not in response.text
