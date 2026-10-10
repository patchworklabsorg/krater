"""Shared helpers for the budget-estimator fieldset on the proposal/amendment draft form
(`projects/new.html`, `projects/edit.html`) and its JSON endpoint (`krater.web.routers.pricing`).

The estimator posts plain strings (a GPU key, hours, basis, margin) that both the no-JS form-submit
path and the JS `fetch` path need to parse and validate the same way -- factored out here so the two
don't drift. See `docs/dev/pricing.md`: the server always recomputes the rate/total itself
(`krater.services.pricing.estimate_cost`); nothing here ever trusts a client-sent dollar figure.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from krater.config import Settings
from krater.services import pricing
from krater.services.errors import ValidationFailed
from krater.web.money import format_cents


def gpu_options(session: Session) -> list[dict[str, str | bool]]:
    """`{key, label, has_spot}` for every currently-priced (accelerator, count), for the estimator's `<select>`."""
    return [
        {
            "key": pricing.gpu_key(row.accelerator_name, row.accelerator_count),
            "label": f"{row.accelerator_name} × {row.accelerator_count}",
            "has_spot": row.spot_min_cents is not None,
        }
        for row in pricing.list_prices(session)
    ]


def default_estimator_values(settings: Settings) -> dict[str, str]:
    """Blank estimator field values for a fresh new/edit form -- no GPU/hours chosen yet, on-demand
    basis, the configured default safety margin."""
    return {
        "estimator_gpu": "",
        "estimator_hours": "",
        "estimator_basis": pricing.BASIS_ON_DEMAND,
        "estimator_margin_percent": str(settings.budget_estimate_default_margin_percent),
    }


def parse_and_estimate(
    session: Session,
    *,
    estimator_gpu: str,
    estimator_hours: str,
    estimator_basis: str,
    estimator_margin_percent: str,
) -> pricing.BudgetEstimate:
    """Validate the raw posted strings and compute the estimate.

    Raises `ValidationFailed` (field name -> message, keyed the same as the form's `name=` attributes)
    for anything wrong -- an empty/unparseable field, an unknown GPU key, or a GPU Krater currently has
    no pricing for -- so callers can always show it as a field error next to the widget rather than a
    generic 500.
    """
    parsed = pricing.parse_gpu_key(estimator_gpu)
    if parsed is None:
        raise ValidationFailed({"estimator_gpu": "Choose a GPU type."})
    accelerator_name, accelerator_count = parsed

    try:
        hours = float(estimator_hours)
        if hours <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationFailed({"estimator_hours": "Enter the number of hours."}) from None

    raw_margin = (estimator_margin_percent or "").strip()
    try:
        margin_percent = int(float(raw_margin)) if raw_margin else 0
        if margin_percent < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationFailed({"estimator_margin_percent": "Enter a whole, non-negative number."}) from None

    return pricing.estimate_cost(
        session,
        accelerator_name=accelerator_name,
        accelerator_count=accelerator_count,
        hours=hours,
        basis=estimator_basis,
        margin_percent=margin_percent,
    )


def summary_text_from_dict(data: dict) -> str:
    """`"1x A100 x 40h at $1.10/hr (on-demand) + 20% margin = $52.80 (prices from Sep 27)"`, per
    `docs/SPEC.md`'s example wording, from a stored `ProjectRevision.budget_estimate` dict (or any
    `BudgetEstimate.as_dict()`). Registered as the `estimate_summary` Jinja filter
    (`krater.web.templates`) so the project detail page can show a reviewer a stored estimate without
    re-fetching pricing."""
    refreshed_at = data["refreshed_at"]
    if isinstance(refreshed_at, str):
        refreshed_at = datetime.fromisoformat(refreshed_at)
    basis_label = "spot" if data["basis"] == pricing.BASIS_SPOT else "on-demand"
    return (
        f"{data['accelerator_count']}× {data['accelerator_name']} × {float(data['hours']):g}h at "
        f"{format_cents(data['rate_cents'])}/hr ({basis_label}) + {data['margin_percent']}% margin = "
        f"{format_cents(data['total_cents'])} (prices from {refreshed_at:%b} {refreshed_at.day})"
    )


def summary_text(result: pricing.BudgetEstimate) -> str:
    return summary_text_from_dict(result.as_dict())


__all__ = [
    "default_estimator_values",
    "gpu_options",
    "parse_and_estimate",
    "summary_text",
    "summary_text_from_dict",
]
