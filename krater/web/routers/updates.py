"""Progress updates and top-up requests on a project (`krater.services.project_updates`).

`GET /projects/{id}/updates/new` is the form; `POST /projects/{id}/updates` posts an update, or, with an amount, a
top-up request, which submits an amendment and so passes the same Slack and demographics gates as a submission. The
project page lists the updates (`projects/detail.html`, `#updates`).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import Project
from krater.services import budget as budget_service
from krater.services import demographics, project_updates
from krater.services.actor import Actor
from krater.services.demographics import DemographicsCheckUnavailable, DemographicsRequired
from krater.services.errors import InvalidState, NotFound, ValidationFailed
from krater.weave import WeaveClient, get_weave_client
from krater.weave.links import demographics_form_url
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor
from krater.web.money import InvalidDollarAmount, parse_dollars
from krater.web.routers.projects import (
    _enforce_slack_membership,
    _get_visible_project,
    _invalid_state_redirect,
    _success_redirect,
)
from krater.web.templates import templates
from krater.worker.app import slack_notify_revision_submitted, slack_post_project_update

router = APIRouter()
Weave = Annotated[WeaveClient, Depends(get_weave_client)]


def _form_context(
    session: Session,
    actor: Actor,
    project: Project,
    *,
    topup: bool,
    posted: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    demographics_url: str | None = None,
) -> dict:
    ceiling = budget_service.ceiling_cents(session, project)
    spent = budget_service.latest_spend_cents(session, project)
    can_topup = project_updates.can_request_topup(project)
    return {
        "project": project,
        "ceiling_cents": ceiling,
        "spent_cents": spent,
        "percent_used": None if ceiling <= 0 else round(spent * 100 / ceiling),
        "can_topup": can_topup,
        "topup": topup and can_topup,
        "posted": posted or {},
        "errors": errors or {},
        "body_max": project_updates.BODY_MAX_CHARS,
        "reason_max": project_updates.REASON_MAX_CHARS,
        # Set when Weave's demographics form is unanswered: it brings the member back to this form.
        "demographics_url": demographics_url,
        "demographics_asked": demographics_url is not None or bool((errors or {}).get("demographics")),
    }


def _postable_project(session: Session, actor: Actor, project_id: uuid.UUID) -> Project:
    project = _get_visible_project(session, actor, project_id)
    if not project_updates.can_post(actor, project):
        # Reviewers and admins can read updates but not post them; nobody posts on a finished project.
        raise NotFound("Only the project's team can post updates, while the project is approved.")
    return project


@router.get("/projects/{project_id}/updates/new")
def new_update(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    topup: str = "",
):
    project = _postable_project(db_session, actor, project_id)
    context = _form_context(db_session, actor, project, topup=topup == "1")
    return templates.TemplateResponse(request, "projects/update_new.html", context)


@router.post("/projects/{project_id}/updates", dependencies=[Depends(verify_csrf_token)])
def post_update(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    weave_client: Weave,
    body: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
    criteria_read: Annotated[str, Form()] = "",
    topup: Annotated[str, Form()] = "",
):
    project = _postable_project(db_session, actor, project_id)
    posted = {"body": body, "amount": amount, "reason": reason}
    wants_topup = topup == "1" or bool(amount.strip()) or bool(reason.strip())

    def _refuse(errors: dict[str, str], demographics_url: str | None = None):
        db_session.rollback()
        context = _form_context(
            db_session,
            actor,
            project,
            topup=wants_topup,
            posted=posted,
            errors=errors,
            demographics_url=demographics_url,
        )
        return templates.TemplateResponse(request, "projects/update_new.html", context, status_code=422)

    if not wants_topup:
        try:
            update = project_updates.post(db_session, actor, project=project, body=body)
        except ValidationFailed as exc:
            return _refuse(exc.errors)
        except InvalidState as exc:
            return _invalid_state_redirect(db_session, request, project.id, exc)
        update_id = update.id
        return _success_redirect(
            db_session,
            request,
            project.id,
            "Update posted. It's on the project page and in the project's Slack channel.",
            after_commit=lambda: slack_post_project_update.defer(update_id=str(update_id)),
        )

    # A top-up request submits an amendment: the same gates as `submit_project`.
    amount_cents: int | None = None
    amount_error = None
    if amount.strip():
        try:
            amount_cents = parse_dollars(amount)
        except InvalidDollarAmount as exc:
            amount_error = str(exc)
    membership_error = _enforce_slack_membership(db_session, actor)
    if membership_error is not None:
        return _refuse({"form": membership_error})
    try:
        demographics.require_answered(weave_client, actor.user)
    except DemographicsRequired as exc:
        back = f"{get_settings().base_url.rstrip('/')}/projects/{project.id}/updates/new?topup=1"
        return _refuse({"form": exc.message, "demographics": "1"}, demographics_url=demographics_form_url(back))
    except DemographicsCheckUnavailable as exc:
        return _invalid_state_redirect(db_session, request, project.id, exc)
    try:
        update = project_updates.request_topup(
            db_session,
            actor,
            project=project,
            amount_cents=amount_cents,
            reason=reason,
            body=body,
            criteria_read=criteria_read == "yes",
        )
    except ValidationFailed as exc:
        errors = dict(exc.errors)
        if amount_error:
            errors["amount"] = amount_error
        return _refuse(errors)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project.id, exc)
    # (An amount that didn't parse reached the service as None, which refused it above.)
    update_id, revision_id = update.id, update.revision_id

    def _notify() -> None:
        slack_notify_revision_submitted.defer(revision_id=str(revision_id))
        slack_post_project_update.defer(update_id=str(update_id))

    return _success_redirect(
        db_session,
        request,
        project.id,
        "Top-up requested. Reviewers decide it like an amendment; your current budget keeps working meanwhile.",
        after_commit=_notify,
    )
