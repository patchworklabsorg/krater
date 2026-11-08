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


def test_a_project_without_links_shows_no_archive_section(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover")

    login_as(REVIEWER_SUB)
    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Archived copies" not in response.text
