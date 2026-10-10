from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB


def test_healthz_runs_a_query_and_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_renders_signed_out(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Krater" in response.text
    assert "Sign in with Weave" in response.text
    assert "/gallery" in response.text


def test_home_page_signed_in_shows_my_projects_and_new_proposal(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    create_project(member, title="My Draft")

    response = client.get("/")

    assert response.status_code == 200
    assert "My Draft" in response.text
    assert "New proposal" in response.text
    assert "Review queue" not in response.text
    assert ">Admin<" not in response.text


def test_home_page_shows_review_queue_link_with_count_for_reviewers(
    client: TestClient, login_as, submitted_project
) -> None:
    member = login_as(MEMBER_SUB)
    submitted_project(member)

    login_as(REVIEWER_SUB)
    response = client.get("/")

    assert "Review queue (1)" in response.text


def test_home_page_shows_admin_link_for_admins(client: TestClient, login_as) -> None:
    login_as(ADMIN_SUB)

    response = client.get("/")

    assert ">Admin<" in response.text
