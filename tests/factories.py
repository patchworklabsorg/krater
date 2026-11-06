"""Test data: a complete application, so tests about something else can create projects that submit."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import Project
from krater.services import projects
from krater.services.actor import Actor


def estimate(total_cents: int = 1_000, *, name: str = "RTX4090", hours: float = 10.0) -> dict:
    """A stored estimate, shaped like `BudgetEstimate.as_dict()`."""
    return {
        "rate_source": "krater_samples",
        "spot_unavailable": False,
        "market_rate_cents": None,
        "accelerator_name": name,
        "accelerator_count": 1,
        "hours": hours,
        "basis": "spot",
        "margin_percent": 0,
        "rate_cents": round(total_cents / hours),
        "subtotal_cents": total_cents,
        "margin_cents": 0,
        "total_cents": total_cents,
        "refreshed_at": "2026-10-11T00:00:00+00:00",
    }


#: Every answer the application needs besides the title, write-up and requested budget.
APPLICATION = {
    "summary": "A small rover that maps caves.",
    "experience": "I build robots.",
    "affiliation": "none",
    "compute_use": "Training a mapping model.",
    "budget_estimate": estimate(),
    "full_budget_cents": 10_000,
    "full_budget_estimate": estimate(10_000, hours=100.0),
}


def new_project(session: Session, actor: Actor, **fields) -> Project:
    """`projects.create_project` with a complete application; `fields` override any answer."""
    return projects.create_project(session, actor, **{**APPLICATION, **fields})
