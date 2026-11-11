"""The stop switch, the organization limits and the spend and storage overview on the admin compute pages."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from krater.models import AuditEvent, ComputeSettings
from krater.services import compute_halt, compute_settings
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token

LIMITS = {
    "org_daily_cap_cents": "0",
    "org_monthly_cap_cents": "0",
    "credit_alert_cents": "0",
    "finished_files_days": "30",
    "credit_reserve_hours": "0",
}


def _row(db_session) -> ComputeSettings:
    compute_halt.current(db_session)
    return db_session.get(ComputeSettings, 1)


def test_the_new_pages_and_actions_are_admin_only(client: TestClient, login_as, create_project, db_session) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member)
    token = get_csrf_token(client.get(f"/projects/{project.id}").text)
    login_as(REVIEWER_SUB)

    assert client.get("/admin/compute/overview").status_code == 403
    for path, data in (
        ("/admin/compute/stop", {"reason": "r"}),
        ("/admin/compute/start", {"reason": "r"}),
        ("/admin/compute/limits", {**LIMITS, "org_daily_cap_cents": "5", "reason": "r"}),
    ):
        assert client.post(path, data={**data, "csrf_token": token}).status_code == 403
    assert _row(db_session).paused_at is None
    assert compute_settings.org_limits(db_session).org_daily_cap_cents == 0


def test_the_overview_needs_sign_in(client: TestClient) -> None:
    response = client.get("/admin/compute/overview", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_an_admin_stops_and_restarts_compute(client: TestClient, login_as, db_session) -> None:
    login_as(ADMIN_SUB)
    page = client.get("/admin/compute")
    assert "Stop all compute" in page.text

    missing = client.post("/admin/compute/stop", data={"reason": " ", "csrf_token": get_csrf_token(page.text)})
    assert missing.status_code == 422 and "A reason is required." in missing.text
    assert _row(db_session).paused_at is None

    response = client.post(
        "/admin/compute/stop",
        data={"reason": "Spend looks wrong.", "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _row(db_session).paused_reason == "Spend looks wrong."
    stopped = client.get("/admin/compute")
    assert "Compute is stopped" in stopped.text and "Turn compute back on" in stopped.text

    response = client.post(
        "/admin/compute/start",
        data={"reason": "Checked: it was a long run.", "csrf_token": get_csrf_token(stopped.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _row(db_session).paused_at is None
    actions = list(db_session.scalars(sa.select(AuditEvent.action).where(AuditEvent.action.like("compute_%ed"))))
    assert compute_halt.AUDIT_PAUSE in actions and compute_halt.AUDIT_RESUME in actions


def test_an_admin_sets_the_organization_limits(client: TestClient, login_as, db_session) -> None:
    login_as(ADMIN_SUB)
    page = client.get("/admin/compute")
    assert 'id="org-org_daily_cap_cents"' in page.text

    response = client.post(
        "/admin/compute/limits",
        data={
            **LIMITS,
            "org_daily_cap_cents": "150.50",
            "credit_alert_cents": "100",
            "reason": "Before compute goes live.",
            "csrf_token": get_csrf_token(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    limits = compute_settings.org_limits(db_session)
    assert (limits.org_daily_cap_cents, limits.credit_alert_cents) == (15050, 10000)
    event = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == compute_settings.AUDIT_ORG)).one()
    assert event.reason == "Before compute goes live."
    assert 'value="150.5"' in client.get("/admin/compute").text


def test_bad_limits_redisplay_the_form(client: TestClient, login_as, db_session) -> None:
    login_as(ADMIN_SUB)
    page = client.get("/admin/compute")

    response = client.post(
        "/admin/compute/limits",
        data={**LIMITS, "finished_files_days": "-1", "reason": "r", "csrf_token": get_csrf_token(page.text)},
    )

    assert response.status_code == 422
    assert "must be between 0 and 3650" in response.text
    assert compute_settings.org_limits(db_session).finished_files_days == 30


def test_the_overview_lists_projects_and_switches_periods(
    client: TestClient, login_as, create_project, db_session
) -> None:
    from datetime import UTC, datetime

    from krater.models import SpendSnapshot, SpendSource

    member = login_as(MEMBER_SUB)
    project = create_project(member, title="Spender")
    db_session.add(
        SpendSnapshot(
            project_id=project.id,
            estimated_spend_cents=1234,
            source=SpendSource.VAST_ESTIMATE,
            taken_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    login_as(ADMIN_SUB)

    for period in ("7d", "30d", "month", "all", "nonsense"):
        page = client.get(f"/admin/compute/overview?period={period}")
        assert page.status_code == 200
        assert "Spender" in page.text and "$12.34" in page.text
    assert "Not read yet" in page.text  # no credit reading yet
    assert 'aria-current="page"' in client.get("/admin/compute/overview?period=all").text
