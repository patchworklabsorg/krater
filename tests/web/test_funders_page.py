"""HTTP-level tests for sharing proposals with OSPool: `/admin/funders/ospool` and its PDF export. The rules are
covered in tests/services/test_funder_packet.py."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import AuditEvent
from krater.services import funder_packet
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_REVIEWER_SUB, REVIEWER_SUB, get_csrf_token

PAGE = "/admin/funders/ospool"


def _exports(db_session: Session) -> list[AuditEvent]:
    return list(db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == funder_packet.AUDIT_EXPORT)))


def _approved(login_as, approved_project, title: str = "Funded rover"):
    return approved_project(login_as(MEMBER_SUB), login_as(OTHER_REVIEWER_SUB), title=title)


def test_an_admin_sees_every_project_unticked(
    client: TestClient, login_as, approved_project, submitted_project, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_us_resident(MEMBER_SUB, True)
    approved = _approved(login_as, approved_project)
    pending = submitted_project(login_as(MEMBER_SUB), title="Waiting rover")

    login_as(ADMIN_SUB)
    response = client.get(PAGE)

    assert response.status_code == 200
    for project in (approved, pending):
        # Unticked: the tag ends straight after the value.
        assert f'name="project_id" value="{project.id}">' in response.text
    assert "Waiting for review" in response.text
    assert "Every team member is a verified US resident" in response.text
    assert "Share with OSPool" in client.get("/admin").text


def test_the_page_says_which_teams_are_not_ospool_ready(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_us_resident(MEMBER_SUB, None)
    _approved(login_as, approved_project)

    login_as(ADMIN_SUB)
    response = client.get(PAGE)

    assert "Not OSPool-ready: 1 of 1 team members aren't verified US residents" in " ".join(response.text.split())


def test_a_reviewer_or_member_may_not_open_the_page(client: TestClient, login_as) -> None:
    login_as(REVIEWER_SUB)
    assert client.get(PAGE).status_code == 403

    login_as(MEMBER_SUB)
    assert client.get(PAGE).status_code == 403


def test_the_page_still_lists_projects_when_weave_is_down(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, monkeypatch
) -> None:
    project = _approved(login_as, approved_project)
    login_as(ADMIN_SUB)
    real_get_user = weave_stub.get_user

    def get_user(sub: str, *, fresh: bool = False):
        if sub == MEMBER_SUB:
            raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    monkeypatch.setattr(weave_stub, "get_user", get_user)
    response = client.get(PAGE)

    assert response.status_code == 200
    assert f'value="{project.id}"' in response.text
    assert "US residency: couldn" in response.text


def test_an_admin_downloads_the_pdf_of_the_ticked_projects_and_it_is_audited(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    chosen = _approved(login_as, approved_project, title="Chosen rover")
    _approved(login_as, approved_project, title="Other rover")
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(f"{PAGE}/export", data={"csrf_token": token, "project_id": [str(chosen.id)]})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith('attachment; filename="ganymede-ospool-')
    assert response.headers["content-disposition"].endswith('.pdf"')
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"%PDF-")
    [event] = _exports(db_session)
    assert event.payload["project_ids"] == [str(chosen.id)]


def test_an_export_with_nothing_ticked_goes_back_with_a_message(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    _approved(login_as, approved_project)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(f"{PAGE}/export", data={"csrf_token": token}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == PAGE
    assert "Tick at least one project to send." in client.get(PAGE).text
    assert _exports(db_session) == []


def test_an_export_fails_closed_when_weave_is_down(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, db_session: Session, monkeypatch
) -> None:
    project = _approved(login_as, approved_project)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)
    real_get_user = weave_stub.get_user

    def get_user(sub: str, *, fresh: bool = False):
        if sub == MEMBER_SUB:
            raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    monkeypatch.setattr(weave_stub, "get_user", get_user)
    response = client.post(
        f"{PAGE}/export",
        data={"csrf_token": token, "project_id": [str(project.id)], "field": list(funder_packet.DEFAULT_FIELDS)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _exports(db_session) == []


def test_a_reviewer_may_not_export(client: TestClient, login_as, approved_project, db_session: Session) -> None:
    project = _approved(login_as, approved_project)
    login_as(REVIEWER_SUB)
    token = get_csrf_token(client.get("/reviews").text)

    response = client.post(f"{PAGE}/export", data={"csrf_token": token, "project_id": [str(project.id)]})

    assert response.status_code == 403
    assert _exports(db_session) == []


def test_an_export_without_the_csrf_token_is_refused(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    project = _approved(login_as, approved_project)
    login_as(ADMIN_SUB)

    response = client.post(f"{PAGE}/export", data={"project_id": [str(project.id)]})

    assert response.status_code == 403
    assert _exports(db_session) == []


def test_the_page_offers_the_fields_with_the_usual_ones_ticked(client: TestClient, login_as, approved_project) -> None:
    _approved(login_as, approved_project)
    login_as(ADMIN_SUB)

    page = client.get(PAGE).text

    for field in funder_packet.FIELDS:
        tick = " checked" if field.default else ""
        assert f'name="field" value="{field.key}"{tick}>' in page


def test_the_export_records_the_ticked_fields(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    chosen = _approved(login_as, approved_project)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(
        f"{PAGE}/export",
        data={"csrf_token": token, "project_id": [str(chosen.id)], "field": ["write_up", "summary"]},
    )

    assert response.status_code == 200
    [event] = _exports(db_session)
    assert event.payload["fields"] == ["summary", "write_up"]


def test_an_unknown_field_goes_back_with_a_message(
    client: TestClient, login_as, approved_project, db_session: Session
) -> None:
    chosen = _approved(login_as, approved_project)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(
        f"{PAGE}/export", data={"csrf_token": token, "project_id": [str(chosen.id)], "field": ["email"]}
    )

    assert response.status_code == 200  # followed back to the page
    assert "Unknown fields: email." in response.text
    assert _exports(db_session) == []
