"""Project pages: create, view, edit the draft, and every contextual action form on the detail page.

Routers are thin: every rule (who may do what, from what state) lives in `krater.services.projects`.
This module's job is parsing form input (including the dollars -> cents and email -> user-id
conversions), calling the service, and turning its domain errors into the right response -- see
`docs/SPEC.md` "Error handling" and CLAUDE.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import (
    Project,
    ProjectRevision,
    ProjectStatus,
    ReviewDecision,
    ReviewSource,
    RevisionKind,
    RevisionOutcome,
    User,
)
from krater.services import pricing, slack_membership
from krater.services import projects as project_service
from krater.services import screenshots as screenshot_service
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.services.launch_policy import LAUNCHABLE_STATUSES
from krater.services.skypilot_sync import current_budget_flag
from krater.slack import get_slack_client
from krater.storage import ObjectStore, get_object_store
from krater.web import estimator_form
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor
from krater.web.flash import flash
from krater.web.forms import UnknownEmails, parse_credited_builder_emails, parse_tags
from krater.web.money import InvalidDollarAmount, cents_to_input, parse_dollars
from krater.web.templates import templates
from krater.worker.app import (
    slack_archive_channel,
    slack_notify_decision,
    slack_notify_revision_submitted,
    slack_post_admin_override,
)

router = APIRouter()

_TERMINAL_STATUSES = (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)

#: `docs/SPEC.md` "Roles & authentication" -- shown when the Slack membership gate blocks a
#: submission. Plain text (flash messages aren't rendered as HTML), with the Weave URL spelled out so
#: it still reads as a link.
_SLACK_MEMBERSHIP_REQUIRED_MESSAGE = (
    "Join the Patchwork Labs Slack and accept the code of conduct before you can submit. "
    "Manage your account at {weave_url}, then try again. If your Slack account uses a different email "
    "from your Weave one, link it in Weave."
)


def _enforce_slack_membership(db_session: Session, actor: Actor) -> str | None:
    """`None` if `actor` passes the Slack membership gate (`docs/SPEC.md` "Roles & authentication"),
    else a user-facing error message to flash. Drafts are always allowed; this is only called from the
    submit routes, right before handing off to `project_service`. A Slack outage raises rather than
    guessing either way."""
    if slack_membership.is_full_slack_member(
        db_session, get_slack_client(), actor.user, weave_slack_member=actor.slack_member
    ):
        return None
    weave_url = get_settings().weave_issuer or "your Weave profile"
    return _SLACK_MEMBERSHIP_REQUIRED_MESSAGE.format(weave_url=weave_url)


# --------------------------------------------------------------------------------------------------
# Visibility: the submitter, reviewers and admins only -- anyone else gets a 404, not a 403, so a
# project's existence doesn't leak to members who have no relationship to it.
# --------------------------------------------------------------------------------------------------


def _can_view(actor: Actor, project: Project) -> bool:
    return actor.user.id == project.submitter_id or actor.is_reviewer or actor.is_admin


def _get_visible_project(session: Session, actor: Actor, project_id: uuid.UUID) -> Project:
    project = project_service.get_project(session, project_id=project_id)
    if not _can_view(actor, project):
        raise NotFound(f"No project with id {project_id}.")
    return project


def _redirect_to_project(project_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _invalid_state_redirect(
    db_session: Session, request: Request, project_id: uuid.UUID, exc: InvalidState
) -> RedirectResponse:
    db_session.rollback()
    flash(request, str(exc), "error")
    return _redirect_to_project(project_id)


def _success_redirect(
    db_session: Session,
    request: Request,
    project_id: uuid.UUID,
    message: str,
    *,
    after_commit: Callable[[], None] | None = None,
) -> RedirectResponse:
    """Commit, run `after_commit` (e.g. deferring a Slack job -- see `docs/SPEC.md` "deferred after the
    web request commits"), flash `message`, and redirect back to the project."""
    db_session.commit()
    if after_commit is not None:
        after_commit()
    flash(request, message, "success")
    return _redirect_to_project(project_id)


# --------------------------------------------------------------------------------------------------
# New project
# --------------------------------------------------------------------------------------------------


def _new_project_values(*, title: str = "", repo_url: str = "", write_up: str = "", budget_requested: str = "") -> dict:
    return {"title": title, "repo_url": repo_url, "write_up": write_up, "budget_requested": budget_requested}


def _estimator_context(
    db_session: Session,
    *,
    estimator_gpu: str = "",
    estimator_hours: str = "",
    estimator_basis: str = "",
    estimator_margin_percent: str = "",
    estimate_summary: str | None = None,
    estimator_used: str = "",
) -> dict:
    """Shared new/edit-form context for the budget estimator fieldset (`_macros.html`'s
    `budget_estimator`). `estimator_values` echoes back whatever was posted so a validation-failure
    round trip doesn't lose the submitter's inputs; falls back to blank defaults on a fresh GET."""
    defaults = estimator_form.default_estimator_values(get_settings())
    return {
        "gpu_options": estimator_form.gpu_options(db_session),
        "estimator_values": {
            "estimator_gpu": estimator_gpu or defaults["estimator_gpu"],
            "estimator_hours": estimator_hours or defaults["estimator_hours"],
            "estimator_basis": estimator_basis or defaults["estimator_basis"],
            "estimator_margin_percent": estimator_margin_percent or defaults["estimator_margin_percent"],
        },
        "estimate_summary": estimate_summary,
        "estimator_used": estimator_used,
    }


def _run_estimate(db_session: Session, **fields: str) -> tuple[pricing.BudgetEstimate | None, dict]:
    """`(result, errors)`: exactly one is truthy. `fields` are the four `estimator_*` posted strings."""
    try:
        return estimator_form.parse_and_estimate(db_session, **fields), {}
    except ValidationFailed as exc:
        return None, exc.errors


def _store_estimate_best_effort(
    db_session: Session, actor: Actor, *, project: Project, estimator_used: str, **fields: str
) -> None:
    """If the submitter used the estimator on this save, recompute it (never trusting anything but the
    choice of GPU/hours/basis/margin -- see `docs/dev/pricing.md`) and store the breakdown. Best-effort:
    a stale/removed GPU key here shouldn't block saving the rest of the draft, so a failure is silently
    skipped rather than surfaced as a field error (unlike the dedicated "Estimate" action, where it is)."""
    if estimator_used != "1":
        return
    result, errors = _run_estimate(db_session, **fields)
    if result is not None:
        project_service.set_budget_estimate(db_session, actor, project=project, budget_estimate=result.as_dict())
    else:
        del errors  # best-effort; see docstring


@router.get("/projects/new")
def new_project_form(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    del actor  # membership itself is enforced by `fresh_actor`; the service checks it again
    context = {"errors": {}, "values": _new_project_values(), **_estimator_context(db_session)}
    return templates.TemplateResponse(request, "projects/new.html", context)


@router.post("/projects/new", dependencies=[Depends(verify_csrf_token)])
def create_project(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    title: Annotated[str, Form()] = "",
    repo_url: Annotated[str, Form()] = "",
    write_up: Annotated[str, Form()] = "",
    budget_requested: Annotated[str, Form()] = "",
    form_action: Annotated[str, Form()] = "save",
    estimator_gpu: Annotated[str, Form()] = "",
    estimator_hours: Annotated[str, Form()] = "",
    estimator_basis: Annotated[str, Form()] = "on_demand",
    estimator_margin_percent: Annotated[str, Form()] = "",
    estimator_used: Annotated[str, Form()] = "",
):
    estimator_fields = dict(
        estimator_gpu=estimator_gpu,
        estimator_hours=estimator_hours,
        estimator_basis=estimator_basis,
        estimator_margin_percent=estimator_margin_percent,
    )
    values = _new_project_values(title=title, repo_url=repo_url, write_up=write_up, budget_requested=budget_requested)

    if form_action == "estimate":
        # The no-JS degrade path (docs/SPEC.md's estimator requirement): recompute right now and
        # re-render with the budget field already filled in, without creating anything yet -- there's
        # no project to attach a draft estimate to before it's created.
        result, errors = _run_estimate(db_session, **estimator_fields)
        estimate_summary = None
        if result is not None:
            values["budget_requested"] = cents_to_input(result.total_cents)
            estimate_summary = estimator_form.summary_text(result)
            estimator_used = "1"
        context = {
            "errors": errors,
            "values": values,
            **_estimator_context(
                db_session, estimate_summary=estimate_summary, estimator_used=estimator_used, **estimator_fields
            ),
        }
        return templates.TemplateResponse(request, "projects/new.html", context, status_code=422 if errors else 200)

    errors: dict[str, str] = {}
    budget_cents = 0
    raw_budget = budget_requested.strip()
    if raw_budget:
        try:
            budget_cents = parse_dollars(raw_budget)
        except InvalidDollarAmount as exc:
            errors["budget_requested_cents"] = str(exc)
    errors.update(project_service.link_errors(repo_url=repo_url))

    if errors:
        context = {
            "errors": errors,
            "values": values,
            **_estimator_context(db_session, estimator_used=estimator_used, **estimator_fields),
        }
        return templates.TemplateResponse(request, "projects/new.html", context, status_code=422)

    project = project_service.create_project(
        db_session,
        actor,
        title=title,
        write_up=write_up,
        budget_requested_cents=budget_cents,
        repo_url=repo_url or None,
    )
    _store_estimate_best_effort(db_session, actor, project=project, estimator_used=estimator_used, **estimator_fields)
    db_session.commit()
    flash(request, "Draft created.", "success")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


# --------------------------------------------------------------------------------------------------
# Detail page: read model + which action forms this viewer may use
# --------------------------------------------------------------------------------------------------


def _build_detail_context(
    session: Session,
    project: Project,
    actor: Actor,
    *,
    errors: dict[str, str] | None = None,
    error_form: str | None = None,
    posted: dict[str, str] | None = None,
) -> dict:
    summary = project_service.project_summary(session, project=project)
    revisions = sorted(project.revisions, key=lambda revision: revision.number, reverse=True)

    reviewer_ids = {review.reviewer_id for revision in revisions for review in revision.reviews}
    builder_ids: set[uuid.UUID] = set()
    for revision in revisions:
        builder_ids.update(revision.credited_builder_ids)
    user_ids = reviewer_ids | builder_ids
    users_by_id = (
        {user.id: user for user in session.scalars(sa.select(User).where(User.id.in_(user_ids)))} if user_ids else {}
    )

    is_submitter = actor.user.id == project.submitter_id
    current = project.current_revision
    has_draft = current is not None and current.submitted_at is None
    current_is_pending = (
        current is not None and current.submitted_at is not None and current.outcome is RevisionOutcome.PENDING
    )
    is_credited_builder = current is not None and actor.user.id in current.credited_builder_ids

    can_review = actor.is_reviewer and not is_submitter and not is_credited_builder and current_is_pending

    skypilot_budget_flag = None
    if project.skypilot_workspace is not None:
        skypilot_budget_flag = current_budget_flag(
            session, project, warn_percent=get_settings().skypilot_budget_warn_percent
        )

    screenshots = _screenshot_entries(get_object_store(), current) if current is not None else []

    # For each revision that used the budget estimator (`budget_estimate` set), whether its actually
    # -requested budget has drifted far enough from that stored estimate to flag for reviewers -- e.g.
    # the submitter estimated one thing, then hand-edited the requested amount well past it.
    threshold_percent = get_settings().budget_estimate_flag_threshold_percent
    estimate_mismatch_by_revision: dict[uuid.UUID, bool] = {}
    for revision in revisions:
        estimate = revision.budget_estimate
        if not estimate:
            continue
        total_cents = estimate.get("total_cents", 0)
        if total_cents > 0:
            diff_percent = abs(revision.budget_requested_cents - total_cents) / total_cents * 100
            estimate_mismatch_by_revision[revision.id] = diff_percent > threshold_percent

    return {
        "project": project,
        "summary": summary,
        "skypilot_budget_flag": skypilot_budget_flag,
        "screenshots": screenshots,
        "revisions": revisions,
        "estimate_mismatch_by_revision": estimate_mismatch_by_revision,
        "users_by_id": users_by_id,
        "is_submitter": is_submitter,
        "is_admin": actor.is_admin,
        # The workspace outlives a finished project until the reconciler tears it down: only show how to launch
        # into it while the launch gate would actually allow it.
        "can_launch": project.skypilot_workspace is not None and project.status in LAUNCHABLE_STATUSES,
        "has_draft": has_draft,
        "can_edit_draft": is_submitter and has_draft,
        "can_submit": is_submitter and has_draft and current.kind is not RevisionKind.COMPLETION,
        "can_submit_completion": is_submitter and has_draft and current.kind is RevisionKind.COMPLETION,
        "can_start_amendment": is_submitter and project.status is ProjectStatus.APPROVED and not has_draft,
        "can_start_completion": is_submitter and project.status is ProjectStatus.APPROVED and not has_draft,
        "can_withdraw": is_submitter and project.status not in _TERMINAL_STATUSES,
        "can_review": can_review,
        # Admin overrides on your own project: deciding it is off entirely, and adding budget is refused by the
        # service (cutting it is still allowed), so the page explains why instead of offering the form.
        "admin_own_project": actor.is_admin and (is_submitter or is_credited_builder),
        "can_admin_decide": actor.is_admin and current_is_pending and not is_submitter and not is_credited_builder,
        "can_admin_adjust_budget": actor.is_admin
        and project.status in (ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW),
        "can_admin_reclaim": actor.is_admin and summary.ceiling_cents > 0,
        "can_admin_withdraw": actor.is_admin and project.status not in _TERMINAL_STATUSES,
        "errors": errors or {},
        "error_form": error_form,
        "posted": posted or {},
    }


@router.get("/projects/{project_id}")
def project_detail(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    context = _build_detail_context(db_session, project, actor)
    return templates.TemplateResponse(request, "projects/detail.html", context)


# --------------------------------------------------------------------------------------------------
# Edit the draft
# --------------------------------------------------------------------------------------------------


def _edit_form_values(session: Session, project: Project, draft: ProjectRevision) -> dict:
    builder_emails = ""
    if draft.credited_builder_ids:
        users = session.scalars(sa.select(User).where(User.id.in_(draft.credited_builder_ids)))
        builder_emails = ", ".join(sorted(user.email for user in users))
    return {
        "title": project.title,
        "repo_url": project.repo_url or "",
        "write_up": draft.write_up,
        "budget_requested": cents_to_input(draft.budget_requested_cents),
        "demo_url": draft.demo_url or "",
        "tags": ", ".join(draft.tags),
        "credited_builder_emails": builder_emails,
    }


def _require_editable_draft(project: Project) -> ProjectRevision | None:
    """The project's current draft, or `None` if there's nothing to edit right now."""
    draft = project.current_revision
    if draft is None or draft.submitted_at is not None:
        return None
    return draft


def _screenshot_entries(store: ObjectStore, draft: ProjectRevision) -> list[dict[str, str]]:
    """Short-lived presigned GET URLs for a draft's screenshots, for the edit page's thumbnails."""
    return [{"key": key, "url": store.presign_download(key)} for key in draft.screenshot_keys]


def _estimator_context_from_draft(db_session: Session, draft: ProjectRevision) -> dict:
    """`_estimator_context`, prefilled from `draft.budget_estimate` if it has one (so reopening the
    edit page still shows what the estimator last computed for this draft), else blank defaults."""
    stored = draft.budget_estimate
    if not stored:
        return _estimator_context(db_session)
    return _estimator_context(
        db_session,
        estimator_gpu=pricing.gpu_key(stored["accelerator_name"], stored["accelerator_count"]),
        estimator_hours=str(stored["hours"]),
        estimator_basis=stored["basis"],
        estimator_margin_percent=str(stored["margin_percent"]),
        estimate_summary=estimator_form.summary_text_from_dict(stored),
    )


@router.get("/projects/{project_id}/edit")
def edit_draft_form(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
):
    project = _get_visible_project(db_session, actor, project_id)
    if actor.user.id != project.submitter_id:
        raise NotAllowed("Only the submitter may edit this project.")

    draft = _require_editable_draft(project)
    if draft is None:
        flash(request, "This project has no draft to edit right now.", "error")
        return _redirect_to_project(project_id)

    values = _edit_form_values(db_session, project, draft)
    context = {
        "project": project,
        "draft": draft,
        "errors": {},
        "values": values,
        **_estimator_context_from_draft(db_session, draft),
    }
    if draft.kind is RevisionKind.COMPLETION:
        context["screenshots"] = _screenshot_entries(store, draft)
        context["max_screenshots"] = screenshot_service.MAX_SCREENSHOTS
        context["max_screenshot_bytes"] = screenshot_service.MAX_SCREENSHOT_BYTES
    return templates.TemplateResponse(request, "projects/edit.html", context)


@router.post("/projects/{project_id}/edit", dependencies=[Depends(verify_csrf_token)])
def update_draft(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
    title: Annotated[str, Form()] = "",
    repo_url: Annotated[str, Form()] = "",
    write_up: Annotated[str, Form()] = "",
    budget_requested: Annotated[str, Form()] = "",
    demo_url: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    credited_builder_emails: Annotated[str, Form()] = "",
    form_action: Annotated[str, Form()] = "save",
    estimator_gpu: Annotated[str, Form()] = "",
    estimator_hours: Annotated[str, Form()] = "",
    estimator_basis: Annotated[str, Form()] = "on_demand",
    estimator_margin_percent: Annotated[str, Form()] = "",
    estimator_used: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    if actor.user.id != project.submitter_id:
        raise NotAllowed("Only the submitter may edit this project.")

    draft = _require_editable_draft(project)
    if draft is None:
        flash(request, "This project has no draft to edit right now.", "error")
        return _redirect_to_project(project_id)

    estimator_fields = dict(
        estimator_gpu=estimator_gpu,
        estimator_hours=estimator_hours,
        estimator_basis=estimator_basis,
        estimator_margin_percent=estimator_margin_percent,
    )
    values = {
        "title": title,
        "repo_url": repo_url,
        "write_up": write_up,
        "budget_requested": budget_requested,
        "demo_url": demo_url,
        "tags": tags,
        "credited_builder_emails": credited_builder_emails,
    }

    if form_action == "estimate":
        result, errors = _run_estimate(db_session, **estimator_fields)
        estimate_summary = None
        if result is not None:
            values["budget_requested"] = cents_to_input(result.total_cents)
            estimate_summary = estimator_form.summary_text(result)
            estimator_used = "1"
        context = {
            "project": project,
            "draft": draft,
            "errors": errors,
            "values": values,
            **_estimator_context(
                db_session, estimate_summary=estimate_summary, estimator_used=estimator_used, **estimator_fields
            ),
        }
        if draft.kind is RevisionKind.COMPLETION:
            context["screenshots"] = _screenshot_entries(store, draft)
            context["max_screenshots"] = screenshot_service.MAX_SCREENSHOTS
            context["max_screenshot_bytes"] = screenshot_service.MAX_SCREENSHOT_BYTES
        return templates.TemplateResponse(request, "projects/edit.html", context, status_code=422 if errors else 200)

    errors: dict[str, str] = {}
    budget_cents = draft.budget_requested_cents
    raw_budget = budget_requested.strip()
    if raw_budget:
        try:
            budget_cents = parse_dollars(raw_budget)
        except InvalidDollarAmount as exc:
            errors["budget_requested_cents"] = str(exc)

    builder_ids = list(draft.credited_builder_ids)
    if draft.kind is RevisionKind.COMPLETION:
        try:
            builder_ids = parse_credited_builder_emails(db_session, credited_builder_emails)
        except UnknownEmails as exc:
            errors["credited_builder_emails"] = f"Unknown email(s): {', '.join(exc.emails)}"
    is_completion = draft.kind is RevisionKind.COMPLETION
    errors.update(project_service.link_errors(repo_url=repo_url, demo_url=demo_url if is_completion else None))

    if errors:
        context = {
            "project": project,
            "draft": draft,
            "errors": errors,
            "values": values,
            **_estimator_context(db_session, estimator_used=estimator_used, **estimator_fields),
        }
        if draft.kind is RevisionKind.COMPLETION:
            context["screenshots"] = _screenshot_entries(store, draft)
            context["max_screenshots"] = screenshot_service.MAX_SCREENSHOTS
            context["max_screenshot_bytes"] = screenshot_service.MAX_SCREENSHOT_BYTES
        return templates.TemplateResponse(request, "projects/edit.html", context, status_code=422)

    update_kwargs: dict = {
        "title": title,
        "repo_url": repo_url or None,
        "write_up": write_up,
        "budget_requested_cents": budget_cents,
    }
    if draft.kind is RevisionKind.COMPLETION:
        update_kwargs.update(demo_url=demo_url or None, tags=parse_tags(tags), credited_builder_ids=builder_ids)

    project_service.update_draft(db_session, actor, project=project, **update_kwargs)
    _store_estimate_best_effort(db_session, actor, project=project, estimator_used=estimator_used, **estimator_fields)
    db_session.commit()
    flash(request, "Draft saved.", "success")
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


# --------------------------------------------------------------------------------------------------
# Submitter actions
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/submit", dependencies=[Depends(verify_csrf_token)])
def submit_project(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)

    membership_error = _enforce_slack_membership(db_session, actor)
    if membership_error is not None:
        db_session.rollback()
        flash(request, membership_error, "error")
        return _redirect_to_project(project_id)

    try:
        project_service.submit(db_session, actor, project=project)
    except ValidationFailed as exc:
        db_session.rollback()
        draft = project.current_revision
        values = _edit_form_values(db_session, project, draft) if draft is not None else {}
        return templates.TemplateResponse(
            request,
            "projects/edit.html",
            {"project": project, "draft": draft, "errors": exc.errors, "values": values},
            status_code=422,
        )
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = project.current_revision_id

    def _notify() -> None:
        slack_notify_revision_submitted.defer(revision_id=str(revision_id))

    return _success_redirect(db_session, request, project_id, "Project submitted for review.", after_commit=_notify)


@router.post("/projects/{project_id}/submit-completion", dependencies=[Depends(verify_csrf_token)])
def submit_completion(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)

    membership_error = _enforce_slack_membership(db_session, actor)
    if membership_error is not None:
        db_session.rollback()
        flash(request, membership_error, "error")
        return _redirect_to_project(project_id)

    try:
        project_service.submit_completion(db_session, actor, project=project)
    except ValidationFailed as exc:
        db_session.rollback()
        draft = project.current_revision
        values = _edit_form_values(db_session, project, draft) if draft is not None else {}
        return templates.TemplateResponse(
            request,
            "projects/edit.html",
            {"project": project, "draft": draft, "errors": exc.errors, "values": values},
            status_code=422,
        )
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = project.current_revision_id

    def _notify() -> None:
        slack_notify_revision_submitted.defer(revision_id=str(revision_id))

    return _success_redirect(db_session, request, project_id, "Completion submitted for review.", after_commit=_notify)


@router.post("/projects/{project_id}/amend", dependencies=[Depends(verify_csrf_token)])
def start_amendment(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        project_service.start_amendment(db_session, actor, project=project)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    db_session.commit()
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


@router.post("/projects/{project_id}/complete", dependencies=[Depends(verify_csrf_token)])
def start_completion(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        project_service.start_completion(db_session, actor, project=project)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    db_session.commit()
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


@router.post("/projects/{project_id}/withdraw", dependencies=[Depends(verify_csrf_token)])
def withdraw_project(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    is_self = actor.user.id == project.submitter_id
    try:
        project_service.withdraw(db_session, actor, project=project, reason=reason or None)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(db_session, project, actor, errors=exc.errors, error_form="withdraw")
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_archive_channel.defer(project_id=str(project.id))
        if not is_self:
            slack_post_admin_override.defer(
                project_id=str(project.id), action="admin_withdraw", actor_name=actor.user.display_name, reason=reason
            )

    return _success_redirect(db_session, request, project_id, "Project withdrawn.", after_commit=_notify)


# --------------------------------------------------------------------------------------------------
# Reviewer action (project-scoped; the review *queue* lives in krater/web/routers/reviews.py)
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/review", dependencies=[Depends(verify_csrf_token)])
def record_review(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        decision_enum = ReviewDecision(decision)
    except ValueError:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors={"decision": "Unknown decision."}, error_form="review"
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    revision = project.current_revision
    try:
        project_service.record_review(
            db_session, actor, revision=revision, decision=decision_enum, reason=reason or None, source=ReviewSource.WEB
        )
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors=exc.errors, error_form="review", posted={"reason": reason}
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = revision.id

    def _notify() -> None:
        # Both are self-guarding no-ops when they don't apply (still pending / not terminal) -- see
        # `krater.services.slack_notify` -- so it's safe to always defer them after any decision.
        slack_notify_decision.defer(revision_id=str(revision_id))
        slack_archive_channel.defer(project_id=str(project.id))

    return _success_redirect(db_session, request, project_id, "Review recorded.", after_commit=_notify)


# --------------------------------------------------------------------------------------------------
# Admin actions
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/admin-decide", dependencies=[Depends(verify_csrf_token)])
def admin_decide(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        decision_enum = ReviewDecision(decision)
    except ValueError:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors={"decision": "Unknown decision."}, error_form="admin_decide"
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    revision = project.current_revision
    try:
        project_service.admin_decide(db_session, actor, revision=revision, decision=decision_enum, reason=reason)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors=exc.errors, error_form="admin_decide", posted={"reason": reason}
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = revision.id
    action = "admin_approve" if decision_enum is ReviewDecision.APPROVE else "admin_reject"

    def _notify() -> None:
        slack_notify_decision.defer(revision_id=str(revision_id))
        slack_archive_channel.defer(project_id=str(project.id))
        slack_post_admin_override.defer(
            project_id=str(project.id), action=action, actor_name=actor.user.display_name, reason=reason
        )

    return _success_redirect(db_session, request, project_id, "Decision recorded.", after_commit=_notify)


@router.post("/projects/{project_id}/admin-budget", dependencies=[Depends(verify_csrf_token)])
def admin_adjust_budget(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    errors: dict[str, str] = {}
    try:
        amount_cents = parse_dollars(amount, allow_negative=True) if amount.strip() else 0
    except InvalidDollarAmount as exc:
        amount_cents = 0
        errors["amount_cents"] = str(exc)

    if errors:
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=errors,
            error_form="admin_budget",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    try:
        project_service.admin_adjust_budget(
            db_session, actor, project=project, amount_cents=amount_cents, reason=reason
        )
    except (ValidationFailed, NotAllowed) as exc:
        # An admin's NotAllowed here is the own-project rule, worth showing next to the amount; anyone else's
        # is plain lack of permission.
        if isinstance(exc, NotAllowed) and not actor.is_admin:
            raise
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=exc.errors if isinstance(exc, ValidationFailed) else {"amount_cents": str(exc)},
            error_form="admin_budget",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_post_admin_override.defer(
            project_id=str(project.id),
            action="admin_adjust_budget",
            actor_name=actor.user.display_name,
            reason=reason,
            extra=f"Amount: {amount_cents / 100:+.2f}",
        )

    return _success_redirect(db_session, request, project_id, "Budget adjusted.", after_commit=_notify)


@router.post("/projects/{project_id}/admin-reclaim", dependencies=[Depends(verify_csrf_token)])
def admin_reclaim_budget(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    errors: dict[str, str] = {}
    try:
        amount_cents = parse_dollars(amount) if amount.strip() else 0
    except InvalidDollarAmount as exc:
        amount_cents = 0
        errors["amount_cents"] = str(exc)

    if errors:
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=errors,
            error_form="admin_reclaim",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    try:
        project_service.reclaim_budget(db_session, actor, project=project, amount_cents=amount_cents, reason=reason)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=exc.errors,
            error_form="admin_reclaim",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_post_admin_override.defer(
            project_id=str(project.id),
            action="admin_reclaim_budget",
            actor_name=actor.user.display_name,
            reason=reason,
            extra=f"Reclaimed: {amount_cents / 100:.2f}",
        )

    return _success_redirect(db_session, request, project_id, "Budget reclaimed.", after_commit=_notify)


# --------------------------------------------------------------------------------------------------
# Screenshots (completion draft only): presign/confirm are called by the edit page's upload widget
# (krater/web/static/js/screenshot-upload.js) via `fetch`, and return JSON rather than a redirect.
# Remove is a plain CSRF-protected form post, so it works with JavaScript disabled.
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/screenshots/presign", dependencies=[Depends(verify_csrf_token)])
def presign_screenshot(
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
    content_type: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        upload = screenshot_service.presign_screenshot(
            db_session, actor, project=project, content_type=content_type, store=store
        )
    except ValidationFailed as exc:
        return JSONResponse({"errors": exc.errors}, status_code=422)
    except InvalidState as exc:
        return JSONResponse({"errors": {"screenshot": str(exc)}}, status_code=409)
    return JSONResponse({"key": upload.key, "url": upload.post.url, "fields": upload.post.fields})


@router.post("/projects/{project_id}/screenshots/confirm", dependencies=[Depends(verify_csrf_token)])
def confirm_screenshot(
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
    key: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        screenshot_service.confirm_screenshot(db_session, actor, project=project, key=key, store=store)
    except ValidationFailed as exc:
        db_session.rollback()
        return JSONResponse({"errors": exc.errors}, status_code=422)
    except InvalidState as exc:
        db_session.rollback()
        return JSONResponse({"errors": {"screenshot": str(exc)}}, status_code=409)
    db_session.commit()
    return JSONResponse({"key": key})


@router.post("/projects/{project_id}/screenshots/{key:path}/delete", dependencies=[Depends(verify_csrf_token)])
def delete_screenshot(
    request: Request,
    project_id: uuid.UUID,
    key: str,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        screenshot_service.remove_screenshot(db_session, actor, project=project, key=key, store=store)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    db_session.commit()
    flash(request, "Screenshot removed.", "success")
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


__all__ = ["router"]
