"""HTTP-level tests for the review queue page, /reviews."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import MEMBER_SUB, REVIEWER_SUB


def test_reviews_page_requires_sign_in(client: TestClient) -> None:
    response = client.get("/reviews", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_non_reviewer_gets_403(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    response = client.get("/reviews")

    assert response.status_code == 403


def test_reviewer_sees_their_queue(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    project = submitted_project(member, title="Needs review")

    login_as(REVIEWER_SUB)
    response = client.get("/reviews")

    assert response.status_code == 200
    assert "Needs review" in response.text
    assert f"/projects/{project.id}" in response.text
