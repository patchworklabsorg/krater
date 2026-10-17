"""Tests for the budget estimator: the JSON endpoint (`/pricing/estimate`), the no-JS "Estimate" submit
on the proposal/amendment draft form, server-side recompute (never trusting a client-sent rate), an
unknown GPU key as a field error, and the stored breakdown shown to reviewers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import GpuPrice, ProjectRevision
from tests.conftest import MEMBER_SUB, get_csrf_token


def _seed_price(
    session: Session, *, name: str = "A100", count: int = 1, median_cents: int = 100, spot_cents=35
) -> GpuPrice:
    price = GpuPrice(
        accelerator_name=name,
        accelerator_count=count,
        vram_gib=40.0,
        vcpus_typical=32.0,
        memory_gib_typical=128.0,
        on_demand_min_cents=median_cents - 5,
        on_demand_median_cents=median_cents,
        spot_min_cents=spot_cents,
        offer_count=2,
        refreshed_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    session.add(price)
    session.flush()
    return price


# --------------------------------------------------------------------------------------------------
# /pricing/estimate (JSON, used by the JS widget)
# --------------------------------------------------------------------------------------------------


def test_estimate_endpoint_requires_sign_in(client: TestClient) -> None:
    # No session at all, so the CSRF check (run as a route dependency) rejects it before `fresh_actor`
    # would even get a chance to redirect -- the same layering every other CSRF-protected POST has.
    response = client.post("/pricing/estimate", data={})
    assert response.status_code == 403


def test_estimate_endpoint_computes_from_current_prices(client: TestClient, login_as, db_session: Session) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/pricing/estimate",
        data={
            "csrf_token": csrf,
            "estimator_gpu": "A100:1",
            "estimator_hours": "40",
            "estimator_basis": "on_demand",
            "estimator_margin_percent": "20",
        },
    )

    assert response.status_code == 200
    body = response.json()
    # 110c * 1 * 40h = 4400c, +20% = 5280c = $52.80
    assert body["total_dollars"] == "52.80"
    assert "A100" in body["summary"]
    assert "$1.10/hr" in body["summary"]


def test_estimate_endpoint_unknown_gpu_key_is_a_field_error(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/pricing/estimate",
        data={
            "csrf_token": csrf,
            "estimator_gpu": "NOT-A-REAL-GPU:1",
            "estimator_hours": "10",
            "estimator_basis": "on_demand",
            "estimator_margin_percent": "20",
        },
    )

    assert response.status_code == 422
    assert "estimator_gpu" in response.json()["errors"]


@pytest.mark.parametrize(
    ("hours", "margin", "field"),
    [
        ("nan", "20", "estimator_hours"),
        ("inf", "20", "estimator_hours"),
        ("1e308", "20", "estimator_hours"),
        ("40", "inf", "estimator_margin_percent"),
        ("40", "1e400", "estimator_margin_percent"),
    ],
)
def test_estimate_endpoint_refuses_non_finite_numbers(
    client: TestClient, login_as, db_session: Session, hours, margin, field
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    csrf = get_csrf_token(client.get("/projects/new").text)

    response = client.post(
        "/pricing/estimate",
        data={
            "csrf_token": csrf,
            "estimator_gpu": "A100:1",
            "estimator_hours": hours,
            "estimator_basis": "on_demand",
            "estimator_margin_percent": margin,
        },
    )

    assert response.status_code == 422
    assert field in response.json()["errors"]


def test_estimate_endpoint_ignores_any_client_sent_rate_or_total(
    client: TestClient, login_as, db_session: Session
) -> None:
    """The endpoint takes no rate/total input at all -- only the choice of GPU/hours/basis/margin -- so
    a spoofed extra field can't influence the computed figure; it's silently ignored like any other
    unrecognized form field."""
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/pricing/estimate",
        data={
            "csrf_token": csrf,
            "estimator_gpu": "A100:1",
            "estimator_hours": "40",
            "estimator_basis": "on_demand",
            "estimator_margin_percent": "20",
            # Not a real field the server reads -- an attempt to smuggle a different total.
            "total_dollars": "0.01",
            "rate_cents": "1",
        },
    )

    assert response.status_code == 200
    assert response.json()["total_dollars"] == "52.80"


# --------------------------------------------------------------------------------------------------
# No-JS "Estimate" submit on /projects/new and /projects/{id}/edit
# --------------------------------------------------------------------------------------------------


def test_new_project_estimate_submit_fills_budget_field_without_creating_a_project(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "write_up": "notes",
            "form_action": "estimate_poc",
            "poc_gpu": "A100:1",
            "poc_hours": "40",
            "poc_basis": "on_demand",
            "poc_margin_percent": "20",
        },
    )

    assert response.status_code == 200
    assert 'value="52.80"' in response.text
    assert "Estimate:" in response.text
    assert "Rover" in response.text  # other typed fields aren't lost


def test_new_project_estimate_submit_unknown_gpu_is_a_field_error(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "form_action": "estimate_poc",
            "poc_gpu": "NOT-A-REAL-GPU:1",
            "poc_hours": "10",
            "poc_basis": "on_demand",
            "poc_margin_percent": "20",
        },
    )

    assert response.status_code == 422
    assert "No current pricing for" in response.text


# --------------------------------------------------------------------------------------------------
# Server-side recompute at save time; stored breakdown; mismatch flag
# --------------------------------------------------------------------------------------------------


def test_save_recomputes_the_estimate_from_current_prices_not_the_earlier_preview(
    client: TestClient, login_as, db_session: Session
) -> None:
    """Even if the price changed between the "Estimate" preview and clicking "Save draft", the stored
    breakdown reflects the rate at *save* time -- never a figure carried over from the client."""
    login_as(MEMBER_SUB)
    price = _seed_price(db_session, name="A100", count=1, median_cents=100)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    # A price update lands between the member previewing an estimate and actually saving the draft.
    price.on_demand_median_cents = 200
    db_session.flush()

    response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "write_up": "notes",
            "poc_budget": "1.00",  # whatever the (stale) preview had filled in, ignored below
            "form_action": "save",
            "poc_gpu": "A100:1",
            "poc_hours": "10",
            "poc_basis": "on_demand",
            "poc_margin_percent": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    project_id = response.headers["location"].removeprefix("/projects/")
    revision = db_session.query(ProjectRevision).filter_by(project_id=project_id).one()
    assert revision.budget_estimate is not None
    # 200c (the *new* price) * 1 * 10h = 2000c -- not 1000c (the stale price).
    assert revision.budget_estimate["rate_cents"] == 200
    assert revision.budget_estimate["total_cents"] == 2000


def test_estimate_breakdown_is_shown_to_reviewers_on_the_project_page(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    create = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "write_up": "notes",
            "form_action": "save",
            "poc_gpu": "A100:1",
            "poc_hours": "40",
            "poc_basis": "on_demand",
            "poc_margin_percent": "20",
        },
        follow_redirects=False,
    )
    project_id = create.headers["location"].removeprefix("/projects/")

    detail = client.get(f"/projects/{project_id}")

    assert detail.status_code == 200
    assert "Estimate:" in detail.text
    assert "$1.10/hr" in detail.text


def test_estimate_mismatch_is_flagged_when_requested_budget_differs_a_lot(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    create = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "write_up": "notes",
            "form_action": "save",
            "poc_gpu": "A100:1",
            "poc_hours": "40",
            "poc_basis": "on_demand",
            "poc_margin_percent": "20",
        },
        follow_redirects=False,
    )
    project_id = create.headers["location"].removeprefix("/projects/")

    edit_page = client.get(f"/projects/{project_id}/edit")
    csrf2 = get_csrf_token(edit_page.text)
    # Estimate was $52.80; hand-override the requested budget to something wildly different.
    client.post(
        f"/projects/{project_id}/edit",
        data={
            "csrf_token": csrf2,
            "title": "Rover",
            "write_up": "notes",
            "poc_budget": "5000.00",
            "poc_gpu": "A100:1",
            "poc_hours": "40",
            "poc_basis": "on_demand",
            "poc_margin_percent": "20",
            "form_action": "save",
        },
        follow_redirects=False,
    )

    detail = client.get(f"/projects/{project_id}")

    assert "The requested amount differs a lot from the estimate." in detail.text


# --------------------------------------------------------------------------------------------------
# No JS: the page still works with the estimator fieldset present but unused
# --------------------------------------------------------------------------------------------------


def test_new_project_page_renders_estimator_fieldset_with_no_pricing_yet(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    response = client.get("/projects/new")

    assert response.status_code == 200
    assert "Proof of concept" in response.text and "Finished product" in response.text
    # With no prices yet, a card can still be estimated at the team's own price.
    assert "Another card (give its price)" in response.text


def test_creating_a_project_without_using_the_estimator_stores_no_estimate(
    client: TestClient, login_as, db_session: Session
) -> None:
    login_as(MEMBER_SUB)
    form = client.get("/projects/new")
    csrf = get_csrf_token(form.text)

    response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Rover",
            "write_up": "notes",
            "poc_budget": "500.00",
            "form_action": "save",
        },
        follow_redirects=False,
    )

    project_id = response.headers["location"].removeprefix("/projects/")
    revision = db_session.query(ProjectRevision).filter_by(project_id=project_id).one()
    assert revision.budget_estimate is None


def test_the_estimator_starts_on_interruptible(client: TestClient, login_as, db_session: Session) -> None:
    # Interruptible is what Krater Compute rents by default, so it's the estimator's default too, catalog or not.
    login_as(MEMBER_SUB)
    _seed_price(db_session, name="A100", count=1, median_cents=110, spot_cents=None)

    page = client.get("/projects/new").text

    assert 'value="spot" selected' in page
    assert "Interruptible (Krater's default)" in page
    assert page.index('value="spot"') < page.index('value="on_demand"')
