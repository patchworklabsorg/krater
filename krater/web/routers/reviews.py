"""The review queue: `/reviews`, reviewers only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.services import projects as project_service
from krater.services.actor import Actor
from krater.web.deps import require_reviewer
from krater.web.templates import templates

router = APIRouter()


@router.get("/reviews")
def review_queue(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_reviewer)],
):
    queue = project_service.review_queue(db_session, actor)
    return templates.TemplateResponse(request, "reviews/queue.html", {"queue": queue})


__all__ = ["router"]
