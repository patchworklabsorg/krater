"""Krater's Help pages (`/help`): the guides in docs/guides, rendered. Public, so a member can read them before
signing in or applying. A guide marked `admins_only` is listed and served only to an admin (others get 404)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from krater.models import User
from krater.services import users as user_service
from krater.web import help_docs
from krater.web.deps import current_user
from krater.web.templates import templates

router = APIRouter()


def _is_admin(user: User | None) -> bool:
    # Display only (cached roles), like the top bar: these pages change nothing.
    return user is not None and user_service.cached_actor(user).is_admin


@router.get("/help", response_class=HTMLResponse)
def help_index(request: Request, user: Annotated[User | None, Depends(current_user)]) -> HTMLResponse:
    is_admin = _is_admin(user)
    guides = [g for g in help_docs.index() if is_admin or not g.guide.admins_only]
    return templates.TemplateResponse(
        request,
        "help/index.html",
        {
            "members": [g for g in guides if g.guide.audience == "members"],
            "admins": [g for g in guides if g.guide.audience == "admins"],
        },
    )


@router.get("/help/{slug}", response_class=HTMLResponse)
def help_guide(request: Request, slug: str, user: Annotated[User | None, Depends(current_user)]) -> HTMLResponse:
    rendered = help_docs.render(slug)
    if rendered is None or (rendered.guide.admins_only and not _is_admin(user)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return templates.TemplateResponse(request, "help/guide.html", {"rendered": rendered})
