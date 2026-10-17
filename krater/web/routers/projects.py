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
from krater.services import budget as budget_service
from krater.services import guardian, people, slack_membership, teams
from krater.services import projects as project_service
from krater.services import screenshots as screenshot_service
from krater.services.actor import Actor
from krater.services.budget_alerts import current_budget_flag
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.services.launch_rules import LAUNCHABLE_STATUSES, hourly_cap_cents
from krater.slack import get_slack_client
from krater.storage import ObjectStore, get_object_store
from krater.weave import WeaveClient, WeaveUnavailableError, get_weave_client
from krater.web import application_form, estimator_form
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor
from krater.web.flash import flash
from krater.web.forms import parse_tags
from krater.web.money import InvalidDollarAmount, cents_to_input, format_cents, parse_dollars
from krater.web.templates import templates
from krater.worker.app import (
    kick_quilt_delivery,
    slack_archive_channel,
    slack_invite_team,
    slack_notify_decision,
    slack_notify_revision_submitted,
    slack_post_admin_override,
)

router = APIRouter()

Weave = Annotated[WeaveClient, Depends(get_weave_client)]

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
# Visibility: the team (and people invited to it), reviewers and admins only -- anyone else gets a 404, not a 403,
# so a project's existence doesn't leak to members who have no relationship to it.
# --------------------------------------------------------------------------------------------------


def _can_view(actor: Actor, project: Project) -> bool:
    return teams.has_conflict(project, actor.user.id) or actor.is_reviewer or actor.is_admin


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
    kick_quilt_delivery()
    flash(request, message, "success")
    return _redirect_to_project(project_id)


# --------------------------------------------------------------------------------------------------
# The application form: new proposals, and editing proposal/amendment drafts (krater.web.application_form)
# --------------------------------------------------------------------------------------------------


def _form_context(
    db_session: Session,
    actor: Actor,
    *,
    values: dict[str, str],
    errors: dict[str, str] | None = None,
    summaries: dict[str, str | None] | None = None,
    team: list[dict] | None = None,
    base_cents: int = 0,
) -> dict:
    return {
        "base_cents": base_cents,
        "values": values,
        "errors": errors or {},
        "summaries": summaries or {"poc": None, "full": None},
        "gpu_options": estimator_form.gpu_options(db_session),
        "applicant": {"name": actor.display_name, "email": actor.email, "sub": actor.user.weave_sub},
        "word_limits": project_service.WORD_LIMITS,
        "summary_max": project_service.SUMMARY_MAX_CHARS,
        "other_gpu": estimator_form.OTHER_GPU,
        "team": team,
    }


def _fill_from_action(form_action: str) -> str | None:
    """The estimator whose "Estimate" button was pressed (the no-JavaScript path), if any."""
    prefix = form_action.removeprefix("estimate_")
    return prefix if form_action.startswith("estimate_") and prefix in application_form.SECTIONS else None


def _check_teammates(weave_client: WeaveClient, raw: str) -> str | None:
    """Why the "Invite teammates" entries can't all be invited, or `None` when every one is a Ganymede member."""
    try:
        unknown = [
            entry for entry in application_form.teammate_entries(raw) if people.find_member(weave_client, entry) is None
        ]
    except WeaveUnavailableError:
        return "Weave isn't answering, so Krater can't look your teammates up. Try again shortly."
    if unknown:
        return f"No Ganymede member has this Patchwork ID or email: {', '.join(unknown)}."
    return None


def _invite_teammates(db_session: Session, actor: Actor, weave_client: WeaveClient, project: Project, raw: str) -> None:
    for entry in application_form.teammate_entries(raw):
        try:
            teams.invite(db_session, actor, weave_client, project=project, query=entry)
        except ValidationFailed as exc:
            raise ValidationFailed({"teammates": f"{entry}: {exc.errors['member']}"}) from None


def _team_rows(weave_client: WeaveClient, session: Session, project: Project) -> list[dict]:
    users = list(session.scalars(sa.select(User).where(User.id.in_(teams.conflict_user_ids(project)))))
    names = people.names(weave_client, users)
    rows = [{"name": names[project.submitter_id], "lead": True}]
    rows += [{"name": names[m.user_id], "lead": False, "accepted": m.accepted} for m in project.members]
    return rows


@router.get("/projects/new")
def new_project_form(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    context = _form_context(db_session, actor, values=application_form.blank_values(get_settings()))
    return templates.TemplateResponse(request, "projects/new.html", context)


@router.post("/projects/new", dependencies=[Depends(verify_csrf_token)])
async def create_project(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
):
    form = await request.form()
    fill = _fill_from_action(str(form.get("form_action", "save")))
    posted = application_form.parse(db_session, form, get_settings(), fill=fill)

    def _again(status_code: int):
        context = _form_context(
            db_session, actor, values=posted.values, errors=posted.errors, summaries=posted.summaries
        )
        return templates.TemplateResponse(request, "projects/new.html", context, status_code=status_code)

    if fill is not None:
        # The no-JS "Estimate" button: show the estimate and its amount without creating anything yet.
        return _again(422 if posted.errors else 200)
    if not posted.errors and (teammates_error := _check_teammates(weave_client, posted.values["teammates"])):
        posted.errors["teammates"] = teammates_error
    if posted.errors:
        return _again(422)

    changes = dict(posted.changes)
    project = project_service.create_project(
        db_session,
        actor,
        title=changes.pop("title"),
        repo_url=changes.pop("repo_url"),
        write_up=changes.pop("write_up"),
        budget_requested_cents=changes.pop("budget_requested_cents"),
        **changes,
    )
    try:
        _invite_teammates(db_session, actor, weave_client, project, posted.values["teammates"])
    except (ValidationFailed, WeaveUnavailableError) as exc:
        db_session.rollback()
        posted.errors["teammates"] = exc.errors["teammates"] if isinstance(exc, ValidationFailed) else str(exc)
        return _again(422)
    db_session.commit()
    flash(request, "Draft saved.", "success")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


def _require_editable_draft(project: Project) -> ProjectRevision | None:
    """The project's current draft, or `None` if there's nothing to edit right now."""
    draft = project.current_revision
    if draft is None or draft.submitted_at is not None:
        return None
    return draft


def _screenshot_entries(store: ObjectStore, draft: ProjectRevision) -> list[dict[str, str]]:
    """Short-lived presigned GET URLs for a draft's screenshots, for the edit page's thumbnails."""
    return [{"key": key, "url": store.presign_download(key)} for key in draft.screenshot_keys]


def _completion_values(project: Project, draft: ProjectRevision) -> dict:
    return {
        "title": project.title,
        "repo_url": project.repo_url or "",
        "write_up": draft.write_up,
        "demo_url": draft.demo_url or "",
        "tags": ", ".join(draft.tags),
    }


def _edit_context(
    db_session: Session,
    weave_client: WeaveClient,
    store: ObjectStore,
    actor: Actor,
    project: Project,
    draft: ProjectRevision,
    *,
    values: dict | None = None,
    errors: dict[str, str] | None = None,
    summaries: dict[str, str | None] | None = None,
) -> dict:
    """The edit page's context: the application form for a proposal or amendment, the completion form otherwise.
    `values` defaults to what the draft holds now."""
    if draft.kind is RevisionKind.COMPLETION:
        return {
            "project": project,
            "draft": draft,
            "values": values or _completion_values(project, draft),
            "errors": errors or {},
            "screenshots": _screenshot_entries(store, draft),
            "max_screenshots": screenshot_service.MAX_SCREENSHOTS,
            "max_screenshot_bytes": screenshot_service.MAX_SCREENSHOT_BYTES,
        }
    return {
        "project": project,
        "draft": draft,
        **_form_context(
            db_session,
            actor,
            values=values or application_form.values_from_draft(project, draft, get_settings()),
            errors=errors,
            summaries=summaries or application_form.summaries_from_draft(draft),
            team=_team_rows(weave_client, db_session, project),
            base_cents=_amendment_base(db_session, project, draft),
        ),
    }


def _amendment_base(session: Session, project: Project, draft: ProjectRevision) -> int:
    """What an amendment's estimate adds to: the budget approved so far. Zero for a proposal."""
    return budget_service.ceiling_cents(session, project) if draft.kind is RevisionKind.AMENDMENT else 0


def _editable(request: Request, db_session: Session, actor: Actor, project_id: uuid.UUID):
    """`(project, draft)` when `actor` may edit the project's draft now; else `(project, None)` after a flash."""
    project = _get_visible_project(db_session, actor, project_id)
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may edit it.")
    draft = _require_editable_draft(project)
    if draft is None:
        flash(request, "This project has no draft to edit right now.", "error")
    return project, draft


@router.get("/projects/{project_id}/edit")
def edit_draft_form(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    store: Annotated[ObjectStore, Depends(get_object_store)],
):
    project, draft = _editable(request, db_session, actor, project_id)
    if draft is None:
        return _redirect_to_project(project_id)
    context = _edit_context(db_session, weave_client, store, actor, project, draft)
    return templates.TemplateResponse(request, "projects/edit.html", context)


@router.post("/projects/{project_id}/edit", dependencies=[Depends(verify_csrf_token)])
async def update_draft(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    store: Annotated[ObjectStore, Depends(get_object_store)],
):
    project, draft = _editable(request, db_session, actor, project_id)
    if draft is None:
        return _redirect_to_project(project_id)
    form = await request.form()

    if draft.kind is RevisionKind.COMPLETION:
        values = {key: str(form.get(key, "")) for key in ("title", "repo_url", "write_up", "demo_url", "tags")}
        errors = project_service.link_errors(repo_url=values["repo_url"], demo_url=values["demo_url"])
        if errors:
            context = _edit_context(
                db_session, weave_client, store, actor, project, draft, values=values, errors=errors
            )
            return templates.TemplateResponse(request, "projects/edit.html", context, status_code=422)
        project_service.update_draft(
            db_session,
            actor,
            project=project,
            title=values["title"],
            repo_url=values["repo_url"],  # "" clears it
            write_up=values["write_up"],
            demo_url=values["demo_url"],  # "" clears it
            tags=parse_tags(values["tags"]),
        )
        db_session.commit()
        flash(request, "Draft saved.", "success")
        return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)

    fill = _fill_from_action(str(form.get("form_action", "save")))
    posted = application_form.parse(
        db_session, form, get_settings(), fill=fill, base_cents=_amendment_base(db_session, project, draft)
    )

    def _again(status_code: int):
        context = _edit_context(
            db_session,
            weave_client,
            store,
            actor,
            project,
            draft,
            values=posted.values,
            errors=posted.errors,
            summaries=posted.summaries,
        )
        return templates.TemplateResponse(request, "projects/edit.html", context, status_code=status_code)

    if fill is not None:
        return _again(422 if posted.errors else 200)
    if not posted.errors and (teammates_error := _check_teammates(weave_client, posted.values["teammates"])):
        posted.errors["teammates"] = teammates_error
    if posted.errors:
        return _again(422)

    project_service.update_draft(db_session, actor, project=project, **posted.changes)
    try:
        _invite_teammates(db_session, actor, weave_client, project, posted.values["teammates"])
    except (ValidationFailed, WeaveUnavailableError) as exc:
        db_session.rollback()
        db_session.refresh(project)
        posted.errors["teammates"] = exc.errors["teammates"] if isinstance(exc, ValidationFailed) else str(exc)
        return _again(422)
    db_session.commit()
    flash(request, "Draft saved.", "success")
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


# --------------------------------------------------------------------------------------------------
# Detail page: read model + which action forms this viewer may use
# --------------------------------------------------------------------------------------------------


def _build_detail_context(
    session: Session,
    weave_client: WeaveClient,
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
    user_ids = reviewer_ids | {project.submitter_id} | {member.user_id for member in project.members}
    users = list(session.scalars(sa.select(User).where(User.id.in_(user_ids))))
    names_by_id = people.names(weave_client, users)
    subs_by_id = {user.id: user.weave_sub for user in users}

    is_lead = actor.user.id == project.submitter_id
    is_team = teams.on_team(project, actor.user.id)
    conflicted = teams.has_conflict(project, actor.user.id)
    team_open = project.status not in teams.CLOSED_STATUSES
    team = [{"name": names_by_id[project.submitter_id], "sub": subs_by_id[project.submitter_id], "lead": True}]
    for member in project.members:
        team.append(
            {
                "id": member.id,
                "name": names_by_id[member.user_id],
                "sub": subs_by_id[member.user_id],
                "accepted": member.accepted,
                "is_me": member.user_id == actor.user.id,
                "can_remove": team_open and (is_lead or actor.is_admin or member.user_id == actor.user.id),
            }
        )
    my_invitation = next((m for m in project.members if m.user_id == actor.user.id and not m.accepted), None)
    current = project.current_revision
    has_draft = current is not None and current.submitted_at is None
    current_is_pending = (
        current is not None and current.submitted_at is not None and current.outcome is RevisionOutcome.PENDING
    )
    can_review = actor.is_reviewer and not conflicted and current_is_pending

    compute_mode = get_settings().compute_mode
    budget_flag = None
    if compute_mode != "off":
        budget_flag = current_budget_flag(
            session,
            project,
            ceiling_cents=budget_service.ceiling_cents(session, project),
            spend_cents=budget_service.latest_spend_cents(session, project),
            warn_percent=get_settings().budget_warn_percent,
        )

    screenshots = _screenshot_entries(get_object_store(), current) if current is not None else []

    # Team members under 18 without a verified guardian in Weave block every budget grant (docs/SPEC.md
    # "Guardian verification"). A finished project gets no more budget, so it doesn't ask. `None` means Weave
    # couldn't say right now; the decision itself still checks Weave fresh.
    guardian_pending = (
        guardian.pending_for_display(session, weave_client, project) if project.status not in _TERMINAL_STATUSES else []
    )

    # For each revision with an estimate, whether the amount it asks for has drifted far enough from that estimate to
    # flag for reviewers -- e.g. the team estimated one thing, then typed a requested amount well past it. An
    # amendment's estimate is the extra compute, so it's compared with what the amendment adds to the ceiling; that's
    # only known while it's undecided, so a decided amendment isn't flagged.
    threshold_percent = get_settings().budget_estimate_flag_threshold_percent
    estimate_mismatch_by_revision: dict[uuid.UUID, bool] = {}
    amendment_adds_cents = None
    if current is not None and current.kind is RevisionKind.AMENDMENT and current.outcome is RevisionOutcome.PENDING:
        amendment_adds_cents = current.budget_requested_cents - summary.ceiling_cents
    for revision in revisions:
        estimate = revision.budget_estimate
        if not estimate or revision.kind is RevisionKind.COMPLETION:
            continue
        asked_cents = revision.budget_requested_cents
        if revision.kind is RevisionKind.AMENDMENT:
            if revision is not current or amendment_adds_cents is None:
                continue
            asked_cents = amendment_adds_cents
        total_cents = estimate.get("total_cents", 0)
        if total_cents > 0:
            diff_percent = abs(asked_cents - total_cents) / total_cents * 100
            estimate_mismatch_by_revision[revision.id] = diff_percent > threshold_percent

    return {
        "project": project,
        "summary": summary,
        "budget_flag": budget_flag,
        "screenshots": screenshots,
        "guardian_pending": guardian_pending,
        "revisions": revisions,
        "estimate_mismatch_by_revision": estimate_mismatch_by_revision,
        "amendment_adds_cents": amendment_adds_cents,
        "names_by_id": names_by_id,
        "is_lead": is_lead,
        "is_team": is_team,
        "team": team,
        "my_invitation": my_invitation,
        "can_invite": team_open and (is_team or actor.is_admin),
        "is_admin": actor.is_admin,
        "show_compute_off_notice": compute_mode == "off" and project.status in LAUNCHABLE_STATUSES,
        "compute_on": compute_mode != "off",
        "has_draft": has_draft,
        "can_edit_draft": is_team and has_draft,
        "can_submit": is_team and has_draft and current.kind is not RevisionKind.COMPLETION,
        "can_submit_completion": is_team and has_draft and current.kind is RevisionKind.COMPLETION,
        "can_start_amendment": is_team and project.status is ProjectStatus.APPROVED and not has_draft,
        "can_start_completion": is_team and project.status is ProjectStatus.APPROVED and not has_draft,
        "can_withdraw": is_lead and project.status not in _TERMINAL_STATUSES,
        "can_review": can_review,
        # Admin overrides on your own project: deciding it is off entirely, and adding budget is refused by the
        # service (cutting it is still allowed), so the page explains why instead of offering the form.
        "admin_own_project": actor.is_admin and conflicted,
        "can_admin_decide": actor.is_admin and current_is_pending and not conflicted,
        "can_admin_adjust_budget": actor.is_admin
        and project.status in (ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW),
        "can_admin_reclaim": actor.is_admin and summary.ceiling_cents > 0,
        "can_admin_withdraw": actor.is_admin and project.status not in _TERMINAL_STATUSES,
        "hourly_cap_cents": hourly_cap_cents(project, get_settings()),
        "hourly_cap_is_default": project.max_hourly_cost_cents is None,
        "hourly_cap_input": ""
        if project.max_hourly_cost_cents is None
        else cents_to_input(project.max_hourly_cost_cents),
        "default_hourly_cap_cents": get_settings().compute_max_hourly_cost_cents,
        "can_admin_set_hourly_cap": actor.is_admin and project.status not in _TERMINAL_STATUSES,
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
    weave_client: Weave,
):
    project = _get_visible_project(db_session, actor, project_id)
    context = _build_detail_context(db_session, weave_client, project, actor)
    return templates.TemplateResponse(request, "projects/detail.html", context)


# --------------------------------------------------------------------------------------------------
# Submitter actions
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/submit", dependencies=[Depends(verify_csrf_token)])
def submit_project(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    store: Annotated[ObjectStore, Depends(get_object_store)],
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
        flash(request, "Some answers are missing or too long. They're marked below.", "error")
        context = _edit_context(
            db_session, weave_client, store, actor, project, project.current_revision, errors=exc.errors
        )
        return templates.TemplateResponse(request, "projects/edit.html", context, status_code=422)
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
    weave_client: Weave,
    store: Annotated[ObjectStore, Depends(get_object_store)],
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
        flash(request, "Some answers are missing or too long. They're marked below.", "error")
        context = _edit_context(
            db_session, weave_client, store, actor, project, project.current_revision, errors=exc.errors
        )
        return templates.TemplateResponse(request, "projects/edit.html", context, status_code=422)
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
    weave_client: Weave,
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    is_self = actor.user.id == project.submitter_id
    try:
        project_service.withdraw(db_session, actor, project=project, reason=reason or None)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, weave_client, project, actor, errors=exc.errors, error_form="withdraw"
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_archive_channel.defer(project_id=str(project.id))
        if not is_self:
            slack_post_admin_override.defer(
                project_id=str(project.id), action="admin_withdraw", actor_sub=actor.user.weave_sub, reason=reason
            )

    return _success_redirect(db_session, request, project_id, "Project withdrawn.", after_commit=_notify)


# --------------------------------------------------------------------------------------------------
# Team: invite, accept or decline, remove or leave (krater.services.teams)
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/team", dependencies=[Depends(verify_csrf_token)])
def invite_team_member(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    member: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        teams.invite(db_session, actor, weave_client, project=project, query=member)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, weave_client, project, actor, errors=exc.errors, error_form="team", posted={"member": member}
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except WeaveUnavailableError:
        db_session.rollback()
        flash(request, "Weave isn't answering, so Krater can't look that person up. Try again shortly.", "error")
        return _redirect_to_project(project_id)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    return _success_redirect(
        db_session, request, project_id, "Invited. They join the team once they accept on their Krater home page."
    )


@router.post("/projects/{project_id}/team/{member_id}/accept", dependencies=[Depends(verify_csrf_token)])
def accept_team_invitation(
    request: Request,
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
):
    try:
        teams.accept(db_session, actor, weave_client, member_id=member_id)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    return _success_redirect(
        db_session,
        request,
        project_id,
        "You're on the team.",
        after_commit=lambda: slack_invite_team.defer(project_id=str(project_id)),
    )


@router.post("/projects/{project_id}/team/{member_id}/decline", dependencies=[Depends(verify_csrf_token)])
def decline_team_invitation(
    request: Request,
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    try:
        teams.decline(db_session, actor, member_id=member_id)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    db_session.commit()
    flash(request, "Invitation declined.", "success")
    return RedirectResponse("/", status_code=303)


@router.post("/projects/{project_id}/team/{member_id}/remove", dependencies=[Depends(verify_csrf_token)])
def remove_team_member(
    request: Request,
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    leaving = any(m.id == member_id and m.user_id == actor.user.id for m in project.members)
    try:
        teams.remove(db_session, actor, project=project, member_id=member_id)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    if leaving:
        db_session.commit()
        flash(request, "You left the team.", "success")
        return RedirectResponse("/", status_code=303)
    return _success_redirect(db_session, request, project_id, "Removed from the team.")


# --------------------------------------------------------------------------------------------------
# Reviewer action (project-scoped; the review *queue* lives in krater/web/routers/reviews.py)
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/review", dependencies=[Depends(verify_csrf_token)])
def record_review(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        decision_enum = ReviewDecision(decision)
    except ValueError:
        db_session.rollback()
        context = _build_detail_context(
            db_session, weave_client, project, actor, errors={"decision": "Unknown decision."}, error_form="review"
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    revision = project.current_revision
    try:
        project_service.record_review(
            db_session,
            actor,
            revision=revision,
            decision=decision_enum,
            reason=reason or None,
            source=ReviewSource.WEB,
            weave_client=weave_client,
        )
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, weave_client, project, actor, errors=exc.errors, error_form="review", posted={"reason": reason}
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
    weave_client: Weave,
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        decision_enum = ReviewDecision(decision)
    except ValueError:
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            weave_client,
            project,
            actor,
            errors={"decision": "Unknown decision."},
            error_form="admin_decide",
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    revision = project.current_revision
    try:
        project_service.admin_decide(
            db_session, actor, revision=revision, decision=decision_enum, reason=reason, weave_client=weave_client
        )
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            weave_client,
            project,
            actor,
            errors=exc.errors,
            error_form="admin_decide",
            posted={"reason": reason},
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
            project_id=str(project.id), action=action, actor_sub=actor.user.weave_sub, reason=reason
        )

    return _success_redirect(db_session, request, project_id, "Decision recorded.", after_commit=_notify)


@router.post("/projects/{project_id}/admin-budget", dependencies=[Depends(verify_csrf_token)])
def admin_adjust_budget(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
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
            weave_client,
            project,
            actor,
            errors=errors,
            error_form="admin_budget",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    try:
        project_service.admin_adjust_budget(
            db_session, actor, project=project, amount_cents=amount_cents, reason=reason, weave_client=weave_client
        )
    except (ValidationFailed, NotAllowed) as exc:
        # An admin's NotAllowed here is the own-project rule, worth showing next to the amount; anyone else's
        # is plain lack of permission.
        if isinstance(exc, NotAllowed) and not actor.is_admin:
            raise
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            weave_client,
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
            actor_sub=actor.user.weave_sub,
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
    weave_client: Weave,
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
            weave_client,
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
            weave_client,
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
            actor_sub=actor.user.weave_sub,
            reason=reason,
            extra=f"Reclaimed: {amount_cents / 100:.2f}",
        )

    return _success_redirect(db_session, request, project_id, "Budget reclaimed.", after_commit=_notify)


@router.post("/projects/{project_id}/admin-hourly-cap", dependencies=[Depends(verify_csrf_token)])
def admin_set_hourly_cap(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    posted = {"amount": amount, "reason": reason}
    # Blank means "back to the default cap".
    cap_cents: int | None = None
    if amount.strip():
        try:
            cap_cents = parse_dollars(amount)
        except InvalidDollarAmount as exc:
            context = _build_detail_context(
                db_session,
                weave_client,
                project,
                actor,
                errors={"cap_cents": str(exc)},
                error_form="admin_hourly_cap",
                posted=posted,
            )
            return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    default_cap_cents = get_settings().compute_max_hourly_cost_cents
    try:
        project_service.set_hourly_cost_cap(
            db_session, actor, project=project, cap_cents=cap_cents, default_cap_cents=default_cap_cents, reason=reason
        )
    except (ValidationFailed, NotAllowed) as exc:
        # As with the budget form: an admin's NotAllowed is the own-project rule, shown by the field.
        if isinstance(exc, NotAllowed) and not actor.is_admin:
            raise
        db_session.rollback()
        errors = exc.errors if isinstance(exc, ValidationFailed) else {"cap_cents": str(exc)}
        context = _build_detail_context(
            db_session, weave_client, project, actor, errors=errors, error_form="admin_hourly_cap", posted=posted
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    new_cap = format_cents(cap_cents if cap_cents is not None else default_cap_cents)
    described = f"{new_cap}/hour" + (" (the default)" if cap_cents is None else "")

    def _notify() -> None:
        slack_post_admin_override.defer(
            project_id=str(project.id),
            action="admin_set_hourly_cap",
            actor_sub=actor.user.weave_sub,
            reason=reason,
            extra=f"Hourly cap per launch: {described}",
        )

    return _success_redirect(db_session, request, project_id, f"Hourly cap set to {described}.", after_commit=_notify)


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
