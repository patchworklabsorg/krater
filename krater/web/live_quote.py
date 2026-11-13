"""Starting a live price check for the job a member is estimating, from the form and the API alike
(`krater.services.live_prices`). The check runs in the worker, which alone talks to Vast; this records the shape,
commits, and defers it."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import Project
from krater.services import compute_settings, live_prices, price_history
from krater.services.compute_jobs import JobSpec
from krater.worker.app import kick_live_quote, kick_live_table


def start(session: Session, project: Project, spec: JobSpec) -> dict | None:
    """The live quote for `spec`'s shape (`live_prices.view`, plus its `key` to poll), asking the worker to check it
    when it's stale; `None` when there's nothing to price (no GPU chosen) or compute isn't on Vast. Commits."""
    if get_settings().compute_mode != "vast" or not (spec.gpu_names or spec.min_gpu_ram_gb is not None):
        return None
    policy = compute_settings.effective_policy(session, project)
    row, needs_check = live_prices.request(session, live_prices.shape_of(spec, policy), now=datetime.now(UTC))
    key = row.key
    shown = live_prices.view(row)
    session.commit()
    if needs_check:
        kick_live_quote(key)
    return {**shown, "key": key}


def start_table(session: Session) -> dict | None:
    """Ask the worker to refresh the form's "Cheapest now" column if it's stale, and return it as it stands
    (`live_prices.view_table`); `None` when compute isn't on Vast. Commits."""
    settings = get_settings()
    if settings.compute_mode != "vast":
        return None
    needs = live_prices.request_table(
        session, settings.price_history_gpus, price_history.RENTAL_TYPES, now=datetime.now(UTC)
    )
    shown = live_prices.view_table(session)
    session.commit()
    if needs:
        kick_live_table()
    return shown


def poll(session: Session, key: str) -> dict | None:
    """The quote `key` names as it stands, for a page or the CLI polling; `None` if there's none."""
    row = live_prices.find(session, key)
    return None if row is None else {**live_prices.view(row), "key": key}
