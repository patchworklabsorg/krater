"""The application form (docs/SPEC.md "The application"): every question, two overridable compute estimators, and
teammates invited as the proposal is saved."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import GpuPrice, Project, ProjectMember, ProjectRevision, ProjectStatus
from krater.services import budget
from krater.services import projects as project_service
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB, get_csrf_token
from tests.factories import estimate

ANSWERS = {
    "title": "Cave Rover",
    "summary": "A rover that maps caves.",
    "experience": "I build robots.",
    "write_up": "It maps caves with lidar.",
    "affiliation": "contest",
    "affiliation_detail": "Example Robotics Cup",
    "compute_use": "Training the mapping model.",
    "poc_gpu": "A100:1",
    "poc_hours": "40",
    "poc_basis": "on_demand",
    "poc_margin_percent": "0",
    "full_gpu": "A100:1",
    "full_hours": "400",
    "full_basis": "on_demand",
    "full_margin_percent": "0",
}


def _seed_price(session: Session, *, name: str = "A100", median_cents: int = 100) -> None:
    session.add(
        GpuPrice(
            accelerator_name=name,
            accelerator_count=1,
            vram_gib=40.0,
            vcpus_typical=32.0,
            memory_gib_typical=128.0,
            on_demand_min_cents=median_cents,
            on_demand_median_cents=median_cents,
            spot_min_cents=None,
            offer_count=2,
            refreshed_at=datetime(2026, 9, 20, tzinfo=UTC),
        )
    )
    session.flush()


def _create(client: TestClient, **fields) -> object:
    csrf = get_csrf_token(client.get("/projects/new").text)
    return client.post("/projects/new", data={"csrf_token": csrf, **ANSWERS, **fields}, follow_redirects=False)


def _revision(db_session: Session, response) -> ProjectRevision:
    project_id = response.headers["location"].removeprefix("/projects/")
    return db_session.scalar(sa.select(ProjectRevision).where(ProjectRevision.project_id == project_id))


def test_the_form_asks_every_question_and_shows_who_is_applying(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    page = client.get("/projects/new").text

    for question in (
        "Who are you, and what&#39;s your experience in this field?",
        "Applying as a group? Invite your teammates",
        "What is your project, in a sentence normal people would understand?",
        "Now explain it properly.",
        "Are you doing this as part of a contest or with the help of a professor?",
        "How are you going to use the compute?",
        "Proof of concept",
        "Finished product",
    ):
        assert question in page
    # Name, email and Patchwork ID come from Weave, not from questions.
    assert "Mia Member" in page and "mia@example.com" in page and MEMBER_SUB in page
    assert "0 of 300 words" in page and "0 of 500 words" in page


def test_saving_stores_every_answer_and_both_estimates(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, median_cents=100)

    response = _create(client)

    assert response.status_code == 303
    revision = _revision(db_session, response)
    assert (revision.summary, revision.affiliation, revision.affiliation_detail) == (
        "A rover that maps caves.",
        "contest",
        "Example Robotics Cup",
    )
    # With no amount typed, each amount is its estimate: $1.00/h x 40h, and x 400h.
    assert revision.budget_requested_cents == 4_000
    assert revision.budget_estimate["total_cents"] == 4_000
    assert revision.full_budget_cents == 40_000
    assert revision.full_budget_estimate["hours"] == 400


def test_the_team_can_request_a_different_amount_and_reviewers_see_both(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, median_cents=100)

    response = _create(client, poc_budget="90.00")

    revision = _revision(db_session, response)
    assert revision.budget_requested_cents == 9_000
    assert revision.budget_estimate["total_cents"] == 4_000
    detail = client.get(response.headers["location"]).text
    assert "$90.00" in detail and "The requested amount differs a lot from the estimate." in detail


def test_the_team_can_price_a_card_krater_has_no_price_for(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)

    response = _create(
        client,
        poc_gpu="other",
        poc_gpu_other="RTX A4000",
        poc_gpu_count="2",
        poc_rate="0.25",
        full_gpu="other",
        full_gpu_other="RTX A4000",
        full_rate="0.25",
    )

    assert response.status_code == 303
    estimate = _revision(db_session, response).budget_estimate
    assert (estimate["accelerator_name"], estimate["accelerator_count"], estimate["rate_cents"]) == ("RTX A4000", 2, 25)
    assert estimate["rate_source"] == "member" and estimate["market_rate_cents"] is None
    # $0.25 x 2 cards x 40h
    assert estimate["total_cents"] == 2_000
    detail = client.get(response.headers["location"]).text
    assert "the team&#39;s own hourly price; Krater has no price for it" in detail


def test_another_card_needs_its_price(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    response = _create(client, poc_gpu="other", poc_gpu_other="RTX A4000")

    assert response.status_code == 422
    assert "Give this card&#39;s price per hour." in response.text


def test_the_teams_price_overrides_krater_s_and_keeps_it_for_comparison(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, median_cents=100)

    response = _create(client, poc_rate="0.50")

    estimate = _revision(db_session, response).budget_estimate
    assert (estimate["rate_cents"], estimate["market_rate_cents"], estimate["total_cents"]) == (50, 100, 2_000)


def test_the_no_javascript_estimate_button_fills_its_own_amount(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, median_cents=100)
    csrf = get_csrf_token(client.get("/projects/new").text)

    response = client.post(
        "/projects/new",
        data={"csrf_token": csrf, **ANSWERS, "poc_budget": "1.00", "form_action": "estimate_full"},
    )

    assert response.status_code == 200
    assert re.search(r'name="full_budget"[^>]*value="400.00"', response.text)
    assert 'value="1.00"' in response.text  # the proof of concept's own amount is the team's
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Project)) == 0


def test_teammates_are_invited_as_the_proposal_is_saved(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session)

    response = _create(client, teammates="max@example.com")

    assert response.status_code == 303
    revision = _revision(db_session, response)
    member = db_session.scalar(sa.select(ProjectMember).where(ProjectMember.project_id == revision.project_id))
    assert member.user.weave_sub == OTHER_MEMBER_SUB and member.accepted_at is None


def test_an_unknown_teammate_saves_nothing(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session)

    response = _create(client, teammates="max@example.com, nobody@example.com")

    assert response.status_code == 422
    assert "No Ganymede member has this Patchwork ID or email: nobody@example.com." in response.text
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Project)) == 0


def test_a_saved_application_reloads_into_the_form_and_submits(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session)
    response = _create(client, poc_rate="0.90")
    project_url = response.headers["location"]

    edit = client.get(f"{project_url}/edit").text
    assert 'value="A rover that maps caves."' in edit
    assert '<option value="A100:1" selected>' in edit
    assert 'value="0.9"' in edit

    detail = client.get(project_url)
    submitted = client.post(
        f"{project_url}/submit", data={"csrf_token": get_csrf_token(detail.text)}, follow_redirects=False
    )
    assert submitted.status_code == 303
    project = project_service.get_project(db_session, project_id=_revision(db_session, response).project_id)
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_submitting_an_unfinished_application_marks_whats_missing(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)
    csrf = get_csrf_token(client.get("/projects/new").text)
    created = client.post("/projects/new", data={"csrf_token": csrf, "title": "Half done"}, follow_redirects=False)
    project_url = created.headers["location"]

    detail = client.get(project_url)
    response = client.post(f"{project_url}/submit", data={"csrf_token": get_csrf_token(detail.text)})

    assert response.status_code == 422
    for message in (
        "Describe the project in one sentence.",
        "Tell reviewers who you are and what experience you have.",
        "Say how you&#39;ll use the compute.",
        "Choose the card and hours a proof of concept needs.",
        "Choose the card and hours the finished product needs (a rough guess is fine).",
    ):
        assert message in response.text


def test_a_price_can_be_a_fraction_of_a_cent(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session)

    response = _create(client, poc_rate="0.0587")

    estimate = _revision(db_session, response).budget_estimate
    # $0.0587 x 40h = $2.348
    assert (estimate["rate_microusd"], estimate["total_cents"]) == (58_700, 235)
    assert "$0.0587/hr" in client.get(response.headers["location"]).text
    assert 'value="0.0587"' in client.get(f"{response.headers['location']}/edit").text

    refused = _create(client, poc_rate="0.05875")
    assert refused.status_code == 422 and "at most four decimal places" in refused.text


def test_an_amendment_adds_its_estimate_to_the_approved_budget(
    client: TestClient, login_as, db_session: Session, approved_project
) -> None:
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    _seed_price(db_session, median_cents=100)
    # Proof of concept approved at $40; the finished product estimated at $400.
    project = approved_project(
        member,
        reviewer,
        budget_requested_cents=4_000,
        budget_estimate=estimate(4_000, name="A100", hours=40.0),
        full_budget_cents=40_000,
        full_budget_estimate=estimate(40_000, name="A100", hours=400.0),
    )
    login_as(MEMBER_SUB)
    page = client.get(f"/projects/{project.id}")
    client.post(f"/projects/{project.id}/amend", data={"csrf_token": get_csrf_token(page.text)})

    # The amendment starts from the finished product's estimate, on top of the $40 approved.
    edit = client.get(f"/projects/{project.id}/edit").text
    assert re.search(r'name="poc_budget"[^>]*value="440.00"', edit)
    assert 'data-base-cents="4000"' in edit

    # Estimating 100 more hours at $1/h fills in the new total: $40 + $100.
    posted = {
        "csrf_token": get_csrf_token(edit),
        **ANSWERS,
        "poc_hours": "100",
        "poc_budget": "999",
        "form_action": "estimate_poc",
    }
    response = client.post(f"/projects/{project.id}/edit", data=posted)
    assert re.search(r'name="poc_budget"[^>]*value="140.00"', response.text)

    saved = client.post(
        f"/projects/{project.id}/edit",
        data={**posted, "poc_budget": "140.00", "form_action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    detail = client.get(f"/projects/{project.id}").text
    assert "adds $100.00 to the $40.00 approved now" in detail
    assert "differs a lot" not in detail

    client.post(f"/projects/{project.id}/submit", data={"csrf_token": get_csrf_token(detail)})
    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}")
    client.post(f"/projects/{project.id}/review", data={"csrf_token": get_csrf_token(page.text), "decision": "approve"})
    db_session.expire_all()
    assert budget.ceiling_cents(db_session, project_service.get_project(db_session, project_id=project.id)) == 14_000
