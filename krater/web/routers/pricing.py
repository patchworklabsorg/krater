"""The public GPU pricing page (`/pricing`, no auth) and the budget estimator's JSON endpoint
(`/pricing/estimate`). See `docs/dev/pricing.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import GpuPriceSample
from krater.services import pricing
from krater.services.actor import Actor
from krater.services.errors import ValidationFailed
from krater.web import estimator_form
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor
from krater.web.templates import templates

router = APIRouter()

_SORT_LABELS = {
    "name": "GPU",
    "price": "Lowest price",
    "price_desc": "Highest price",
    "vram": "VRAM",
    "count": "Count",
    "offers": "Offer count",
}


@router.get("/pricing")
def pricing_index(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    sort: str = pricing.DEFAULT_SORT,
    gpu: str = "",
):
    settings = get_settings()
    accelerator_names = pricing.list_accelerator_names(db_session)
    gpu_filter = gpu if gpu in accelerator_names else ""
    rows = pricing.list_prices(db_session, sort=sort, accelerator_name=gpu_filter or None)
    # Interruptible prices come from Krater's own samples (the catalog has none): the typical 1-GPU bid of the last
    # week, times the row's GPU count.
    now = datetime.now(UTC)
    sampled_bid: dict[str, int | None] = {}
    for name in {row.accelerator_name for row in rows}:
        sampled = pricing.sampled_rate_cents(db_session, accelerator_name=name, basis=pricing.BASIS_SPOT, now=now)
        sampled_bid[name] = sampled[0] if sampled else None
    # Per row: Krater's sampled bid for the row's GPU count, or else the catalog's lowest spot price, if it has one.
    interruptible = {
        (row.accelerator_name, row.accelerator_count): (
            sampled_bid[row.accelerator_name] * row.accelerator_count
            if sampled_bid[row.accelerator_name] is not None
            else row.spot_min_cents
        )
        for row in rows
    }

    return templates.TemplateResponse(
        request,
        "pricing/index.html",
        {
            "rows": rows,
            "interruptible": interruptible,
            "accelerator_names": accelerator_names,
            "selected_gpu": gpu_filter,
            "sort": sort if sort in _SORT_LABELS else pricing.DEFAULT_SORT,
            "sort_labels": _SORT_LABELS,
            "refreshed_at": pricing.last_refreshed_at(db_session),
            # Krater's own Vast price history only fills while compute runs (the worker needs a Vast key).
            "show_history": settings.compute_on
            or db_session.scalar(sa.select(sa.exists().where(GpuPriceSample.id.is_not(None)))),
            "cap_cents": settings.compute_max_hourly_cost_cents,
        },
    )


@router.post("/pricing/estimate", dependencies=[Depends(verify_csrf_token)])
def estimate(
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    estimator_gpu: Annotated[str, Form()] = "",
    estimator_hours: Annotated[str, Form()] = "",
    estimator_basis: Annotated[str, Form()] = pricing.BASIS_ON_DEMAND,
    estimator_margin_percent: Annotated[str, Form()] = "",
    estimator_rate: Annotated[str, Form()] = "",
    estimator_gpu_other: Annotated[str, Form()] = "",
    estimator_gpu_count: Annotated[str, Form()] = "",
):
    """JSON estimate for the application form's compute estimators
    (`krater/web/static/js/budget-estimator.js`). Signed-in members only, like every other draft-form
    action; needs no project (the new-project form has none yet). Recomputes entirely server-side --
    see `krater.services.pricing.estimate_cost` -- this never trusts a client-sent rate or total.
    """
    del actor  # membership is enough here; there's no project-scoped authorization to check
    try:
        result = estimator_form.parse_and_estimate(
            db_session,
            estimator_gpu=estimator_gpu,
            estimator_hours=estimator_hours,
            estimator_basis=estimator_basis,
            estimator_margin_percent=estimator_margin_percent,
            estimator_rate=estimator_rate,
            estimator_gpu_other=estimator_gpu_other,
            estimator_gpu_count=estimator_gpu_count,
        )
    except ValidationFailed as exc:
        return JSONResponse({"errors": exc.errors}, status_code=422)

    return JSONResponse(
        {"total_dollars": f"{result.total_cents / 100:.2f}", "summary": estimator_form.summary_text(result)}
    )


__all__ = ["router"]
