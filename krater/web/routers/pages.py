"""Publicly reachable pages: the home page and the health check."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import User
from krater.services import projects as project_service
from krater.services import users as user_service
from krater.web.deps import current_user
from krater.web.templates import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    user: Annotated[User | None, Depends(current_user)],
) -> HTMLResponse:
    if user is None:
        return templates.TemplateResponse(request, "home.html", {"signed_in": False})

    # Display/navigation only (which links to show, and a rough count) -- every action those links
    # lead to re-checks Weave via `fresh_actor` before it does anything.
    actor = user_service.cached_actor(user)
    my_projects = project_service.list_projects_for_user(db_session, user_id=user.id)
    review_queue_count = len(project_service.review_queue(db_session, actor)) if actor.is_reviewer else 0

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "signed_in": True,
            "my_projects": my_projects,
            "is_reviewer": actor.is_reviewer,
            "is_admin": actor.is_admin,
            "review_queue_count": review_queue_count,
        },
    )


@router.get("/healthz")
def healthz(session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
