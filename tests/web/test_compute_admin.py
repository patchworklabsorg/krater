"""HTTP-level tests for the compute settings pages: /admin/compute and /admin/compute/projects/{id}."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.models import AuditEvent
from krater.services import compute_settings
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token

FORM = {
    "reliability_floor_bp": "0.95",
    "bid_margin_percent": "10",
    "pause_grace_minutes": "5",
    "move_after_minutes": "30",
    "box_idle_minutes": "30",
    "box_destroy_hours": "24",
    "reconcile_alert_percent": "10",
    "runner_start_minutes": "10",
    "heartbeat_timeout_minutes": "30",
    "runner_self_stop_minutes": "180",
    "box_sync_deadline_minutes": "10",
    "box_resume_timeout_minutes": "10",
    "storage_quota_gb": "50",
}


def test_compute_settings_require_sign_in(client: TestClient) -> None:
    response = client.get("/admin/compute", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_non_admins_get_403(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)
    login_as(REVIEWER_SUB)

    assert client.get("/admin/compute").status_code == 403
    assert client.get(f"/admin/compute/projects/{project.id}").status_code == 403


def test_non_admins_cannot_post(client: TestClient, login_as, create_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)
    page = client.get(f"/projects/{project.id}")
    token = get_csrf_token(page.text)

    response = client.post(
        "/admin/compute", data={**FORM, "bid_margin_percent": "50", "reason": "r", "csrf_token": token}
    )
    assert response.status_code == 403
    response = client.post(
        f"/admin/compute/projects/{project.id}", data={"bid_margin_percent": "50", "reason": "r", "csrf_token": token}
    )
    assert response.status_code == 403
    assert compute_settings.global_policy(db_session).bid_margin_percent == 10


def test_admin_sees_and_saves_the_defaults(client: TestClient, login_as, db_session) -> None:
    login_as(ADMIN_SUB)
    page = client.get("/admin/compute")
    assert page.status_code == 200
    assert 'value="0.95"' in page.text
    assert "Every project follows the defaults." in page.text

    response = client.post(
        "/admin/compute",
        data={
            **FORM,
            "reliability_floor_bp": "0.97",
            "reason": "Too many flaky hosts.",
            "csrf_token": get_csrf_token(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert compute_settings.global_policy(db_session).reliability_floor_bp == 9700
    event = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == compute_settings.AUDIT_GLOBAL)).one()
    assert event.reason == "Too many flaky hosts."


def test_bad_values_redisplay_the_form(client: TestClient, login_as, db_session) -> None:
    login_as(ADMIN_SUB)
    page = client.get("/admin/compute")

    response = client.post(
        "/admin/compute",
        data={**FORM, "box_idle_minutes": "two", "reason": "", "csrf_token": get_csrf_token(page.text)},
    )

    assert response.status_code == 422
    assert "Enter a number for box idle." in response.text
    assert 'value="two"' in response.text
    assert compute_settings.global_policy(db_session).box_idle_minutes == 30


def test_admin_overrides_one_project(client: TestClient, login_as, create_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Override Me")
    login_as(ADMIN_SUB)

    detail = client.get(f"/projects/{project.id}")
    assert f"/admin/compute/projects/{project.id}" in detail.text
    page = client.get(f"/admin/compute/projects/{project.id}")
    assert page.status_code == 200
    assert "Default: 0.95" in page.text

    response = client.post(
        f"/admin/compute/projects/{project.id}",
        data={"bid_margin_percent": "20", "reason": "Needs to hold its bid.", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    policy = compute_settings.effective_policy(db_session, project)
    assert policy.bid_margin_percent == 20
    assert policy.overridden == {"bid_margin_percent"}
    overview = client.get("/admin/compute")
    assert "Override Me" in overview.text
    assert "Bid margin: 20 %" in overview.text


def test_project_override_errors_redisplay(client: TestClient, login_as, create_project) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)
    login_as(ADMIN_SUB)
    page = client.get(f"/admin/compute/projects/{project.id}")

    response = client.post(
        f"/admin/compute/projects/{project.id}",
        data={
            "move_after_minutes": "2",
            "pause_grace_minutes": "10",
            "reason": "r",
            "csrf_token": get_csrf_token(page.text),
        },
    )

    assert response.status_code == 422
    assert "at least the pause grace" in response.text


def test_unknown_project_is_a_404(client: TestClient, login_as) -> None:
    login_as(ADMIN_SUB)

    assert client.get(f"/admin/compute/projects/{uuid.uuid4()}").status_code == 404


def test_admin_sees_spend_checks_that_did_not_match(client: TestClient, login_as, create_project, db_session) -> None:
    from datetime import UTC, datetime

    from krater.models import SpendReconciliation

    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Mismatched")
    db_session.add(
        SpendReconciliation(
            project_id=project.id,
            checked_at=datetime(2026, 10, 11, 13, 7, tzinfo=UTC),
            instance_count=1,
            estimate_microusd=64_000,
            actual_microusd=100_000,
            alerted=True,
            details=[],
        )
    )
    db_session.flush()
    login_as(ADMIN_SUB)

    page = client.get("/admin/compute").text

    assert "Mismatched" in page
    assert "$0.0640" in page and "$0.1000" in page


def test_the_page_shows_vasts_search_quota(client: TestClient, login_as, db_session) -> None:
    from datetime import UTC, datetime, timedelta

    from krater.compute import search_quota
    from krater.vast import SearchQuota

    login_as(ADMIN_SUB)
    assert "searched Vast yet." in client.get("/admin/compute").text

    now = datetime.now(UTC)
    reset = now + timedelta(hours=3)
    search_quota.record(db_session, SearchQuota(limit=20_000, remaining=0, reset_at=reset), now=now)
    db_session.commit()

    page = client.get("/admin/compute").text
    assert "<strong>0 of 20,000</strong> search rows left" in page
    assert "no job or box can start until the reset" in page
