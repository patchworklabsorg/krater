"""Shared helpers for the compute estimators on the application form (`projects/_application.html`: one for the
proof of concept, one for the finished product) and their JSON endpoint (`krater.web.routers.pricing`).

An estimator posts plain strings (a GPU key, or another card's name and count, hours, basis, margin, and an optional
hourly price of the team's own) that the no-JS form-submit path and the JS `fetch` path parse and validate the same
way. See `docs/dev/pricing.md`: the server always recomputes the total itself
(`krater.services.pricing.estimate_cost`). The team can override two things, both visible to reviewers: the hourly
price (which the estimate then uses) and the amount they request.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from krater.config import Settings
from krater.services import pricing
from krater.services.errors import ValidationFailed
from krater.web.money import format_cents

#: The GPU `<select>` value for a card Krater has no price for; the team names it and gives its hourly price.
OTHER_GPU = "other"
# Far above any real proposal; they keep nonsense like "1e308" out of the arithmetic.
MAX_ESTIMATE_HOURS = 100_000
MAX_MARGIN_PERCENT = 1000
#: The posted fields of one estimator, without its prefix (`estimator_` on the JSON endpoint, `poc_`/`full_` on the
#: application form).
FIELDS = ("gpu", "gpu_other", "gpu_count", "hours", "basis", "margin_percent", "rate")


def gpu_options(session: Session) -> list[dict[str, str | bool]]:
    """`{key, label, has_spot}` for every currently-priced (accelerator, count), for the estimator's `<select>`.
    `has_spot`: Krater has an interruptible price for it (its own samples; the catalog has none today)."""
    now = datetime.now(UTC)
    sampled: dict[str, bool] = {}
    options = []
    for row in pricing.list_prices(session):
        if row.accelerator_name not in sampled:
            sampled[row.accelerator_name] = (
                pricing.sampled_rate_cents(
                    session, accelerator_name=row.accelerator_name, basis=pricing.BASIS_SPOT, now=now
                )
                is not None
            )
        options.append(
            {
                "key": pricing.gpu_key(row.accelerator_name, row.accelerator_count),
                "label": f"{row.accelerator_name} × {row.accelerator_count}",
                "has_spot": row.spot_min_cents is not None or sampled[row.accelerator_name],
            }
        )
    return options


def default_estimator_values(settings: Settings, prefix: str = "estimator") -> dict[str, str]:
    """Blank field values for one estimator on a fresh form -- no GPU/hours chosen yet, interruptible basis (what
    Krater Compute rents by default), the configured default safety margin, Krater's own hourly price."""
    values = {f"{prefix}_{field}": "" for field in FIELDS}
    values[f"{prefix}_basis"] = pricing.BASIS_SPOT
    values[f"{prefix}_margin_percent"] = str(settings.budget_estimate_default_margin_percent)
    values[f"{prefix}_gpu_count"] = "1"
    return values


def values_from_estimate(estimate: dict | None, settings: Settings, prefix: str) -> dict[str, str]:
    """The estimator's field values that reproduce a stored estimate (`BudgetEstimate.as_dict()`), for an edit form.
    A card that isn't in Krater's price list comes back as "another card"."""
    values = default_estimator_values(settings, prefix)
    if not estimate:
        return values
    name, count = estimate["accelerator_name"], int(estimate["accelerator_count"])
    from_member = estimate.get("rate_source") == pricing.SOURCE_MEMBER
    if from_member and estimate.get("market_rate_cents") is None:
        values[f"{prefix}_gpu"] = OTHER_GPU
        values[f"{prefix}_gpu_other"] = name
        values[f"{prefix}_gpu_count"] = str(count)
    else:
        values[f"{prefix}_gpu"] = pricing.gpu_key(name, count)
    values[f"{prefix}_hours"] = f"{float(estimate['hours']):g}"
    values[f"{prefix}_basis"] = pricing.BASIS_SPOT if estimate.get("spot_unavailable") else estimate["basis"]
    values[f"{prefix}_margin_percent"] = str(estimate["margin_percent"])
    if from_member:
        microusd = estimate.get("rate_microusd") or estimate["rate_cents"] * 10_000
        values[f"{prefix}_rate"] = f"{(Decimal(microusd) / 1_000_000).normalize():f}"
    return values


def is_blank(fields: dict[str, str], prefix: str = "estimator") -> bool:
    """Whether the team hasn't started this estimator: no card and no hours."""
    return not fields.get(f"{prefix}_gpu", "").strip() and not fields.get(f"{prefix}_hours", "").strip()


def parse_prefixed(session: Session, fields: dict[str, str], prefix: str) -> pricing.BudgetEstimate:
    """`parse_and_estimate` for the estimator whose fields start with `prefix` (`poc`, `full`). Errors come back
    keyed by those same field names."""
    try:
        return parse_and_estimate(
            session, **{f"estimator_{field}": fields.get(f"{prefix}_{field}", "") for field in FIELDS}
        )
    except ValidationFailed as exc:
        raise ValidationFailed(
            {key.replace("estimator_", f"{prefix}_", 1): message for key, message in exc.errors.items()}
        ) from None


def parse_hourly_rate(raw: str) -> int:
    """A price per card-hour typed in dollars (`0.0587`, `$1.20`) as integer micro-dollars. Up to four decimal places,
    since bids differ by fractions of a cent. Raises `ValueError` with a message to show next to the field."""
    text = raw.strip().removeprefix("$").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("Enter a price like 0.25 or 0.0587.") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("Enter an hourly price above zero, or leave it blank.")
    if value.as_tuple().exponent < -4:
        raise ValueError("Use at most four decimal places, like 0.0587.")
    if value > 100:
        raise ValueError("That's more than $100 per card-hour.")
    return int(value * 1_000_000)


def _rate_text(data: dict) -> str:
    microusd = data.get("rate_microusd")
    if microusd is None:
        return format_cents(data["rate_cents"])
    return (
        f"${(Decimal(microusd) / 1_000_000).normalize():f}" if microusd % 10_000 else format_cents(microusd // 10_000)
    )


def parse_and_estimate(
    session: Session,
    *,
    estimator_gpu: str,
    estimator_hours: str,
    estimator_basis: str,
    estimator_margin_percent: str,
    estimator_rate: str = "",
    estimator_gpu_other: str = "",
    estimator_gpu_count: str = "",
) -> pricing.BudgetEstimate:
    """Validate the raw posted strings and compute the estimate.

    `estimator_gpu` is a `pricing.gpu_key`, or `OTHER_GPU` with the card named in `estimator_gpu_other` and counted
    in `estimator_gpu_count`; another card needs `estimator_rate`, the team's price per GPU-hour in dollars, which
    otherwise is optional and replaces Krater's own.

    Raises `ValidationFailed` (field name -> message, keyed the same as the form's `name=` attributes)
    for anything wrong -- an empty/unparseable field, an unknown GPU key, or a GPU Krater currently has
    no pricing for -- so callers can always show it as a field error next to the widget rather than a
    generic 500.
    """
    errors: dict[str, str] = {}
    rate_override_microusd = None
    if estimator_rate.strip():
        try:
            rate_override_microusd = parse_hourly_rate(estimator_rate)
        except ValueError as exc:
            errors["estimator_rate"] = str(exc)
    if estimator_gpu == OTHER_GPU:
        accelerator_name = " ".join(estimator_gpu_other.split())[:64]
        if not accelerator_name:
            errors["estimator_gpu_other"] = "Name the card."
        try:
            accelerator_count = int(estimator_gpu_count or "1")
            if not 1 <= accelerator_count <= 16:
                raise ValueError
        except ValueError:
            errors["estimator_gpu_count"] = "Between 1 and 16 cards."
            accelerator_count = 1
        if not estimator_rate.strip():
            errors["estimator_rate"] = "Give this card's price per hour."
    else:
        parsed = pricing.parse_gpu_key(estimator_gpu)
        if parsed is None:
            errors["estimator_gpu"] = "Choose a GPU type."
            accelerator_name, accelerator_count = "", 0
        else:
            accelerator_name, accelerator_count = parsed
    if errors:
        raise ValidationFailed(errors)

    try:
        hours = float(estimator_hours)
        if not math.isfinite(hours) or not 0 < hours <= MAX_ESTIMATE_HOURS:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationFailed({"estimator_hours": "Enter the number of hours."}) from None

    raw_margin = (estimator_margin_percent or "").strip()
    try:
        margin_percent = int(float(raw_margin)) if raw_margin else 0
        if not 0 <= margin_percent <= MAX_MARGIN_PERCENT:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValidationFailed({"estimator_margin_percent": "Enter a whole, non-negative number."}) from None

    return pricing.estimate_cost(
        session,
        accelerator_name=accelerator_name,
        accelerator_count=accelerator_count,
        hours=hours,
        basis=estimator_basis,
        margin_percent=margin_percent,
        rate_override_microusd=rate_override_microusd,
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
    basis_label = "interruptible" if data["basis"] == pricing.BASIS_SPOT else "on-demand"
    if data.get("spot_unavailable"):
        basis_label = "on-demand; no interruptible prices for this GPU yet"
    if data.get("rate_source") == pricing.SOURCE_MEMBER:
        market = data.get("market_rate_cents")
        source = "the team's own hourly price" + (
            f"; Krater's typical is {format_cents(market)}/hr" if market is not None else "; Krater has no price for it"
        )
    elif data.get("rate_source") == pricing.SOURCE_SAMPLES:
        source = f"Krater's Vast prices, median of the week to {refreshed_at:%b} {refreshed_at.day}"
    else:
        source = f"prices from {refreshed_at:%b} {refreshed_at.day}"
    return (
        f"{data['accelerator_count']}× {data['accelerator_name']} × {float(data['hours']):g}h at "
        f"{_rate_text(data)}/hr ({basis_label}) + {data['margin_percent']}% margin = "
        f"{format_cents(data['total_cents'])} ({source})"
    )


def summary_text(result: pricing.BudgetEstimate) -> str:
    return summary_text_from_dict(result.as_dict())


__all__ = [
    "FIELDS",
    "OTHER_GPU",
    "default_estimator_values",
    "gpu_options",
    "is_blank",
    "parse_and_estimate",
    "parse_prefixed",
    "values_from_estimate",
    "summary_text",
    "summary_text_from_dict",
]
