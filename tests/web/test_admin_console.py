"""The admin console: the Admin link in the navigation bar, search and status filter on /admin, the activity feed
(/admin/activity, and the recent events on /admin), and live compute (/admin/live)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeInstance, ComputeInstanceState, ComputeJobState
from krater.services import audit, compute_jobs
from krater.services import projects as project_service
from krater.services.compute_jobs import JobSpec
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, REVIEWER_SUB
from tests.web.conftest import actor_for

NAV_LINK = '<a class="site-nav__link" href="/admin"'


# --------------------------------------------------------------------------------------------------
# The Admin link
# --------------------------------------------------------------------------------------------------


def test_admins_see_the_admin_link_in_the_navigation(client: TestClient, login_as) -> None:
    login_as(ADMIN_SUB)

    assert NAV_LINK in client.get("/").text
    assert 'href="/admin" aria-current="page">Admin</a>' in client.get("/admin").text


@pytest.mark.parametrize("sub", [MEMBER_SUB, REVIEWER_SUB])
def test_members_and_reviewers_dont_see_it(client: TestClient, login_as, sub) -> None:
    login_as(sub)

    assert NAV_LINK not in client.get("/").text


def test_signed_out_visitors_dont_see_it(client: TestClient) -> None:
    assert NAV_LINK not in client.get("/").text


# --------------------------------------------------------------------------------------------------
# Search and filter on /admin
# --------------------------------------------------------------------------------------------------


def test_the_project_table_searches_titles_and_lead_ids(client: TestClient, login_as, submitted_project) -> None:
    member = login_as(MEMBER_SUB)
    submitted_project(member, title="Protein folding")
    submitted_project(member, title="Weather model")
    login_as(ADMIN_SUB)

    found = client.get("/admin", params={"q": "PROTEIN"}).text
    assert "Protein folding" in found
    assert "Weather model" not in found

    by_lead = client.get("/admin", params={"q": MEMBER_SUB.lower()}).text
    assert "Protein folding" in by_lead and "Weather model" in by_lead

    nothing = client.get("/admin", params={"q": "no such thing"}).text
    assert "No project matches." in nothing


def test_the_project_table_filters_by_status(client: TestClient, login_as, submitted_project, create_project) -> None:
    member = login_as(MEMBER_SUB)
    submitted_project(member, title="Waiting one")
    create_project(member, title="Still a draft")
    login_as(ADMIN_SUB)

    response = client.get("/admin", params={"status": "draft"})

    assert "Still a draft" in response.text
    assert "Waiting one" not in response.text
    assert '<option value="draft" selected>' in response.text


def test_an_unknown_status_shows_every_project(client: TestClient, login_as, submitted_project) -> None:
    submitted_project(login_as(MEMBER_SUB), title="Waiting one")
    login_as(ADMIN_SUB)

    assert "Waiting one" in client.get("/admin", params={"status": "bogus"}).text


# --------------------------------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def adjusted(login_as, approved_project, db_session, weave_stub):
    """An approved project an admin then added $25.00 to."""
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    admin = login_as(ADMIN_SUB)
    project_service.admin_adjust_budget(
        db_session,
        actor_for(admin),
        project=project,
        amount_cents=2_500,
        reason="Extra GPU hours for the demo",
        weave_client=weave_stub,
    )
    db_session.flush()
    return project


def test_the_activity_page_needs_an_admin(client: TestClient, login_as) -> None:
    assert client.get("/admin/activity", follow_redirects=False).status_code == 303
    login_as(REVIEWER_SUB)
    assert client.get("/admin/activity").status_code == 403
    assert client.get("/admin/live").status_code == 403


def test_the_activity_page_lists_admin_overrides(client: TestClient, adjusted) -> None:
    response = client.get("/admin/activity")

    assert response.status_code == 200
    assert "Admin adjust budget" in response.text
    assert "Extra GPU hours for the demo" in response.text
    assert "amount: +$25.00" in response.text
    assert adjusted.title in response.text


def test_the_admin_overview_shows_recent_activity(client: TestClient, adjusted) -> None:
    response = client.get("/admin")

    assert "Recent activity" in response.text
    assert "Extra GPU hours for the demo" in response.text


def test_a_projects_activity_shows_its_budget_ledger(client: TestClient, adjusted) -> None:
    response = client.get("/admin/activity", params={"project": str(adjusted.id)})

    assert response.status_code == 200
    assert "Budget ledger" in response.text
    assert "Admin adjustment" in response.text
    assert "+$25.00" in response.text
    assert "Approval" in response.text
    assert "Ceiling now $125.00" in response.text  # the $100.00 approval plus the $25.00


def test_the_project_page_links_admins_to_its_ledger(client: TestClient, adjusted) -> None:
    assert f'href="/admin/activity?project={adjusted.id}"' in client.get(f"/projects/{adjusted.id}").text


def test_an_unknown_project_is_404(client: TestClient, login_as) -> None:
    login_as(ADMIN_SUB)

    assert client.get("/admin/activity", params={"project": "00000000-0000-0000-0000-000000000000"}).status_code == 404


def test_activity_pages(client: TestClient, adjusted, db_session, monkeypatch) -> None:
    from krater.web.routers import admin_activity

    audit.record(db_session, None, "budget_warning", project=adjusted, payload={"remaining_cents": 1_000})
    db_session.flush()
    monkeypatch.setattr(admin_activity, "PAGE_SIZE", 1)

    first = client.get("/admin/activity").text
    assert "Older &rarr;" in first
    assert "&larr; Newer" not in first
    second = client.get("/admin/activity", params={"page": 2}).text
    assert "&larr; Newer" in second


# --------------------------------------------------------------------------------------------------
# Live compute
# --------------------------------------------------------------------------------------------------


def _job(db_session, project, member, name):
    return compute_jobs.submit_job(
        db_session,
        actor_for(member),
        project=project,
        spec=JobSpec(
            name=name,
            image="ghcr.io/alice/train:v3",
            command="python train.py",
            gpu_names=("RTX 3060",),
            target_microusd=70_000,
            max_microusd=100_000,
        ),
        cap_cents=500,
        now=datetime.now(UTC),
        weave_client=StubWeaveClient(),
    )


def test_live_compute_lists_running_and_queued_jobs_across_projects(
    client: TestClient, login_as, approved_project, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    member = login_as(MEMBER_SUB)
    project = approved_project(member, login_as(REVIEWER_SUB))
    _job(db_session, project, member, "waiting-train")
    running = _job(db_session, project, member, "busy-train")
    running.state = ComputeJobState.RUNNING
    db_session.add(
        ComputeInstance(
            job=running,
            attempt=1,
            label="krtest-live-1",
            state=ComputeInstanceState.RUNNING,
            provider_offer_id="1",
            gpu_name="RTX 3060",
            interruptible=True,
            gpu_microusd=70_000,
            disk_microusd=5_000,
        )
    )
    db_session.flush()
    login_as(ADMIN_SUB)

    response = client.get("/admin/live")

    assert response.status_code == 200
    assert "waiting-train" in response.text
    assert "busy-train" in response.text
    assert "RTX 3060" in response.text
    assert f'href="/projects/{project.id}/compute/{running.id}"' in response.text
    assert "Compute is off here" not in response.text


def test_live_compute_says_when_compute_is_off(client: TestClient, login_as) -> None:
    login_as(ADMIN_SUB)

    response = client.get("/admin/live")

    assert response.status_code == 200
    assert "Compute is off here" in response.text
    assert "Nothing is running." in response.text
