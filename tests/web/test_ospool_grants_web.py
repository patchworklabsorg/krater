"""The web side of OSPool grants: recording one, its project page card, its valuation and the admin overview."""

from __future__ import annotations

from datetime import UTC, date, datetime

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import AuditEvent, OspoolGrant
from krater.ospool.accounting import FakeAccounting
from krater.services import budget, grants, ospool_grants
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.web.test_application_form import _seed_price

NAME = "PatchworkLabs_Ganymede01"


def _post(client: TestClient, url: str, page_url: str, **data):
    csrf = get_csrf_token(client.get(page_url).text)
    return client.post(url, data={"csrf_token": csrf, **data}, follow_redirects=False)


def _held(client, login_as, submitted_project, db_session, weave_stub):
    """A project approved as awaiting a 3rd party review, its team all verified US residents."""
    _seed_price(db_session, name="A100", median_cents=150)
    member = login_as(MEMBER_SUB)
    project = submitted_project(member)
    login_as(REVIEWER_SUB)
    _post(
        client,
        f"/projects/{project.id}/review",
        f"/projects/{project.id}",
        decision="approve",
        funding_hold="awaiting_third_party_review",
    )
    weave_stub.set_us_resident(MEMBER_SUB, True)
    db_session.refresh(project)
    return project


def _record(client, project, **fields):
    data = {
        "ospool_project_name": NAME,
        "counted_from": "2026-11-01",
        "gpu_name": "A100",
        "rate": "",
        "rate_note": "",
        "reason": "OSPool approved it.",
    }
    data.update(fields)
    return _post(client, f"/projects/{project.id}/admin-ospool-grant", f"/projects/{project.id}", **data)


def test_an_admin_records_a_grant_and_the_page_reports_it(
    client: TestClient, login_as, submitted_project, db_session: Session, weave_stub
):
    project = _held(client, login_as, submitted_project, db_session, weave_stub)
    login_as(ADMIN_SUB)
    assert "Record an OSPool grant" in client.get(f"/projects/{project.id}").text

    response = _record(client, project, rate="2.49", rate_note="A neocloud's posted price")

    assert response.status_code == 303
    grant = db_session.scalar(sa.select(OspoolGrant).where(OspoolGrant.project_id == project.id))
    assert grant.rate_override_microusd == 2_490_000
    assert grants.pending(db_session, project) is None
    assert budget.ceiling_cents(db_session, project) == 0
    client_accounting = FakeAccounting()
    client_accounting.set_day(NAME, date(2026, 11, 3), gpu_hours="4", core_hours="32", jobs=7)
    ospool_grants.import_usage(db_session, grant, client_accounting, now=datetime(2026, 11, 5, tzinfo=UTC))

    page = client.get(f"/projects/{project.id}").text
    assert "OSPool grant recorded." in page
    assert "Approved · OSPool" in page
    assert NAME in page
    assert "$9.96" in page  # 4 GPU-hours at $2.49
    assert "OSPool valuation" in page
    assert "Record an OSPool grant" not in page
    admin_page = client.get("/admin").text
    assert "OSPool grants" in admin_page
    assert NAME in admin_page
    assert "Approved · OSPool" in admin_page

    login_as(MEMBER_SUB)
    team_page = client.get(f"/projects/{project.id}").text
    assert "OSPool grant" in team_page
    assert "$9.96" in team_page
    assert "OSPool valuation" not in team_page
    assert "Approved · OSPool" in client.get("/").text


def test_a_reviewer_cant_record_one(client, login_as, submitted_project, db_session, weave_stub):
    project = _held(client, login_as, submitted_project, db_session, weave_stub)
    login_as(REVIEWER_SUB)

    response = _record(client, project)

    assert response.status_code == 403
    assert db_session.scalar(sa.select(OspoolGrant)) is None


def test_bad_input_shows_on_the_form(client, login_as, submitted_project, db_session, weave_stub):
    project = _held(client, login_as, submitted_project, db_session, weave_stub)
    db_session.commit()  # the refused post rolls back to here
    login_as(ADMIN_SUB)

    response = _record(client, project, ospool_project_name="not a name", rate="abc", counted_from="", reason="")

    # Every problem at once: the form's own (price, day) and the service's (name, reason).
    assert response.status_code == 422
    assert "Enter a price like 0.25 or 0.0587." in response.text
    assert "as OSG registered it" in response.text
    assert "Enter the day usage counts from." in response.text
    assert "A reason is required." in response.text
    assert db_session.scalar(sa.select(OspoolGrant)) is None


def test_a_team_not_all_us_residents_is_refused(client, login_as, submitted_project, db_session, weave_stub):
    project = _held(client, login_as, submitted_project, db_session, weave_stub)
    weave_stub.set_us_resident(MEMBER_SUB, False)
    db_session.commit()
    login_as(ADMIN_SUB)

    response = _record(client, project)

    assert response.status_code == 303
    assert db_session.scalar(sa.select(OspoolGrant)) is None
    assert grants.pending(db_session, project) is not None


def test_an_admin_changes_the_valuation(client, login_as, submitted_project, db_session, weave_stub):
    project = _held(client, login_as, submitted_project, db_session, weave_stub)
    login_as(ADMIN_SUB)
    _record(client, project)

    response = _post(
        client,
        f"/projects/{project.id}/admin-ospool-valuation",
        f"/projects/{project.id}",
        gpu_name="A100",
        rate="1.99",
        rate_note="Another neocloud",
        reason="Cheaper price found.",
    )

    assert response.status_code == 303
    grant = db_session.scalar(sa.select(OspoolGrant))
    assert grant.rate_override_microusd == 1_990_000
    assert "Valuation saved." in client.get(f"/projects/{project.id}").text
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == ospool_grants.AUDIT_VALUATION))
    assert event.reason == "Cheaper price found."


def test_a_member_cant_change_the_valuation(client, login_as, submitted_project, db_session, weave_stub):
    project = _held(client, login_as, submitted_project, db_session, weave_stub)
    login_as(ADMIN_SUB)
    _record(client, project)
    login_as(MEMBER_SUB)

    response = _post(
        client,
        f"/projects/{project.id}/admin-ospool-valuation",
        f"/projects/{project.id}",
        gpu_name="A100",
        rate="0.01",
        rate_note="Mine",
        reason="Cheaper.",
    )

    assert response.status_code == 403
    assert db_session.scalar(sa.select(OspoolGrant)).rate_override_microusd is None


def test_the_pdf_leaves_ospool_usage_out_of_a_project_without_a_grant() -> None:
    import uuid

    from krater.models import ProjectStatus
    from krater.services.funder_packet import Packet, PacketRow
    from krater.web.funder_export import row_fields

    def row(usage: str) -> PacketRow:
        return PacketRow(
            project_id=uuid.uuid4(),
            title="Rover",
            summary="A rover.",
            compute_use="",
            training_data="",
            status=ProjectStatus.APPROVED,
            requested_cents=0,
            granted_cents=0,
            gpu=None,
            team_names=[],
            unnamed=0,
            team_size=1,
            all_us_residents=True,
            ospool_usage=usage,
        )

    packet = Packet(
        funder="ospool", generated_at=datetime(2026, 11, 7, tzinfo=UTC), rows=[], fields=("summary", "ospool_usage")
    )

    assert row_fields(packet, row("")) == [("Summary", "A rover.")]
    assert row_fields(packet, row("X1: 1.0 GPU-hours.")) == [
        ("Summary", "A rover."),
        ("OSPool usage", "X1: 1.0 GPU-hours."),
    ]
