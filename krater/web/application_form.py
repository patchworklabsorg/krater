"""The application form (`projects/new.html`, `projects/edit.html`): reading what was posted, and filling the form
back in from a draft.

The questions are in docs/SPEC.md "The application". Two of them are compute estimates, each its own estimator
(`krater.web.estimator_form`, prefixes `poc` and `full`) with an amount the team can override: the proof of concept's
amount is what approval funds (`budget_requested_cents`); the finished product's is for reviewers' information.
Nothing is required while drafting; `krater.services.projects.application_errors` checks the whole application on
submit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import Project, ProjectRevision
from krater.services import projects as project_service
from krater.services.errors import ValidationFailed
from krater.web import estimator_form
from krater.web.money import InvalidDollarAmount, cents_to_input, parse_dollars

#: The plain answers, by form field name.
ANSWERS = ("title", "repo_url", "summary", "experience", "write_up", "affiliation", "affiliation_detail", "compute_use")
#: Estimator prefix -> the draft fields it fills (estimate, amount).
SECTIONS = {"poc": ("budget_estimate", "budget_requested_cents"), "full": ("full_budget_estimate", "full_budget_cents")}


@dataclass
class Posted:
    """A parsed form: `values` to show it again, `errors` by field, and `changes` for `projects.update_draft`."""

    values: dict[str, str]
    errors: dict[str, str] = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    summaries: dict[str, str | None] = field(default_factory=dict)


def _estimator_keys() -> list[str]:
    return [f"{prefix}_{name}" for prefix in SECTIONS for name in estimator_form.FIELDS]


def blank_values(settings: Settings) -> dict[str, str]:
    values = {name: "" for name in ANSWERS}
    values["affiliation"] = "none"
    for prefix in SECTIONS:
        values |= estimator_form.default_estimator_values(settings, prefix)
        values[f"{prefix}_budget"] = ""
    values["teammates"] = ""
    return values


def values_from_draft(project: Project, draft: ProjectRevision, settings: Settings) -> dict[str, str]:
    values = blank_values(settings)
    values |= {
        "title": project.title,
        "repo_url": project.repo_url or "",
        "summary": draft.summary,
        "experience": draft.experience,
        "write_up": draft.write_up,
        "affiliation": draft.affiliation,
        "affiliation_detail": draft.affiliation_detail,
        "compute_use": draft.compute_use,
    }
    for prefix, (estimate_field, amount_field) in SECTIONS.items():
        values |= estimator_form.values_from_estimate(getattr(draft, estimate_field), settings, prefix)
        amount = getattr(draft, amount_field)
        values[f"{prefix}_budget"] = cents_to_input(amount) if amount else ""
    return values


def summaries_from_draft(draft: ProjectRevision) -> dict[str, str | None]:
    return {
        prefix: estimator_form.summary_text_from_dict(estimate)
        if (estimate := getattr(draft, estimate_field))
        else None
        for prefix, (estimate_field, _amount) in SECTIONS.items()
    }


def parse(
    session: Session, form: Mapping[str, str], settings: Settings, *, fill: str | None = None, base_cents: int = 0
) -> Posted:
    """Read the posted application. `fill` names an estimator (`poc`, `full`) whose amount should be replaced by its
    estimate: the no-JavaScript "Estimate" button.

    `base_cents` is the budget approved so far, for an amendment: its first estimate is the extra compute the next
    stage needs, while its amount is the project's new total, so a filled-in amount is `base_cents` plus the
    estimate. The total can still be typed lower (an amendment may cut the budget)."""
    keys = [*ANSWERS, *_estimator_keys(), *(f"{prefix}_budget" for prefix in SECTIONS), "teammates"]
    values = blank_values(settings) | {key: str(form[key]) for key in keys if key in form}
    posted = Posted(values=values)
    changes = posted.changes

    for name in ANSWERS:
        if name not in ("title", "repo_url"):
            changes[name] = values[name]
    changes["title"] = values["title"]
    changes["repo_url"] = values["repo_url"]  # "" clears it; update_draft takes None as "unchanged"
    if values["affiliation"] not in project_service.AFFILIATIONS:
        posted.errors["affiliation"] = "Choose one of the answers."
    if len(values["summary"]) > project_service.SUMMARY_MAX_CHARS:
        posted.errors["summary"] = f"Keep it to one sentence: {project_service.SUMMARY_MAX_CHARS} characters at most."
    posted.errors |= project_service.link_errors(repo_url=values["repo_url"])

    for prefix, (estimate_field, amount_field) in SECTIONS.items():
        estimate = None
        if not estimator_form.is_blank(values, prefix):
            try:
                estimate = estimator_form.parse_prefixed(session, values, prefix)
            except ValidationFailed as exc:
                posted.errors |= exc.errors
        posted.summaries[prefix] = estimator_form.summary_text(estimate) if estimate else None
        if estimate is not None and (fill == prefix or not values[f"{prefix}_budget"].strip()):
            base = base_cents if prefix == "poc" else 0
            values[f"{prefix}_budget"] = cents_to_input(base + estimate.total_cents)

        amount = None
        raw = values[f"{prefix}_budget"].strip()
        if raw:
            try:
                amount = parse_dollars(raw)
            except InvalidDollarAmount as exc:
                posted.errors[f"{prefix}_budget"] = str(exc)
        changes[estimate_field] = estimate.as_dict() if estimate is not None else None
        changes[amount_field] = amount if prefix == "full" else (amount or 0)
    return posted


def teammate_entries(raw: str) -> list[str]:
    """The Patchwork IDs or emails typed into "Invite teammates", one per comma or line."""
    return [entry.strip() for entry in raw.replace("\n", ",").split(",") if entry.strip()]


__all__ = [
    "ANSWERS",
    "SECTIONS",
    "Posted",
    "blank_values",
    "parse",
    "summaries_from_draft",
    "teammate_entries",
    "values_from_draft",
]
