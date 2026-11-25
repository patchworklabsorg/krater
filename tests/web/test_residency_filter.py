"""HTTP-level tests for the OSPool residency filter: the review queue can show only projects whose whole team is
verified US residents, and the project page tells reviewers. The rule itself is covered in
tests/services/test_residency.py."""

from __future__ import annotations

from fastapi.testclient import TestClient

from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, OTHER_REVIEWER_SUB, REVIEWER_SUB


def test_the_queue_filters_to_teams_of_us_residents(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_us_resident(MEMBER_SUB, True)
    resident_project = submitted_project(login_as(MEMBER_SUB), title="Resident rover")
    other_project = submitted_project(login_as(OTHER_MEMBER_SUB), title="Other rover")

    login_as(REVIEWER_SUB)
    everything = client.get("/reviews").text
    filtered = client.get("/reviews?us_residents=true").text

    assert "Resident rover" in everything and "Other rover" in everything
    assert f"/projects/{resident_project.id}" in filtered
    assert f"/projects/{other_project.id}" not in filtered


def test_the_project_page_names_who_is_not_a_verified_us_resident(
    client: TestClient, login_as, submitted_project
) -> None:
    project = submitted_project(login_as(MEMBER_SUB), title="Rover")

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text

    assert "Not all US residents" in page
    assert MEMBER_SUB in page
    assert "OSPool" not in page  # reviewers don't see the partner named either


def test_a_member_does_not_see_the_residency_line(client: TestClient, login_as, submitted_project) -> None:
    project = submitted_project(login_as(MEMBER_SUB), title="Rover")

    page = client.get(f"/projects/{project.id}").text

    assert "OSPool" not in page


def test_the_filter_also_lists_approved_projects_of_us_residents(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_us_resident(MEMBER_SUB, True)
    member = login_as(MEMBER_SUB)
    approver = login_as(OTHER_REVIEWER_SUB)
    project = approved_project(member, approver, title="Funded rover")

    login_as(REVIEWER_SUB)
    page = client.get("/reviews?us_residents=true").text

    assert "Approved projects with a whole team of verified US residents" in page
    assert f"/projects/{project.id}" in page


def test_the_filter_says_so_when_weave_is_down(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient, monkeypatch
) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Rover")
    real_get_user = weave_stub.get_user
    calls: list[str] = []

    def get_user(sub: str, *, fresh: bool = False):
        if sub == member.weave_sub:
            calls.append(sub)
            raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    login_as(REVIEWER_SUB)
    monkeypatch.setattr(weave_stub, "get_user", get_user)
    response = client.get("/reviews?us_residents=true")

    assert response.status_code == 200
    assert "couldn&#39;t reach Weave" in response.text or "couldn't reach Weave" in response.text
    assert f"/projects/{project.id}" not in response.text
    assert len(calls) == 1
