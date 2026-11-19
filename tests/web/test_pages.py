from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB


def test_healthz_runs_a_query_and_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_says_which_commit_is_live(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("KRATER_REVISION", "e5caa36" + "0" * 33)
    assert client.get("/healthz").json() == {"status": "ok", "revision": "e5caa36" + "0" * 33}


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


def test_healthz_worker_answers_503_once_the_worker_heartbeat_is_stale(client: TestClient, db_session) -> None:
    from datetime import UTC, datetime, timedelta

    import sqlalchemy as sa

    from krater.services import ops_alerts

    assert client.get("/healthz/worker").status_code == 503  # never beat
    ops_alerts.beat(db_session, now=datetime.now(UTC) - timedelta(minutes=1))
    db_session.commit()
    fresh = client.get("/healthz/worker")
    assert fresh.status_code == 200 and fresh.json()["status"] == "ok"

    db_session.execute(sa.text("UPDATE worker_heartbeat SET at = now() - interval '10 minutes'"))
    db_session.commit()
    stale = client.get("/healthz/worker")
    assert stale.status_code == 503 and stale.json()["status"] == "stale"


def test_healthz_edge_records_alastors_ping_signed_out(client: TestClient, db_session) -> None:
    import sqlalchemy as sa

    from krater.services import network_watch

    response = client.get("/healthz/edge")

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    first = network_watch.last_edge_ping(db_session)
    assert first is not None

    # Within the write interval a ping changes nothing; after it, the time moves on.
    client.get("/healthz/edge")
    db_session.expire_all()
    assert network_watch.last_edge_ping(db_session) == first
    db_session.execute(sa.text("UPDATE edge_ping SET at = at - interval '1 minute'"))
    db_session.commit()
    client.get("/healthz/edge")
    db_session.expire_all()
    assert network_watch.last_edge_ping(db_session) > first - timedelta(minutes=1)
