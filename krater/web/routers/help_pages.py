"""Krater's Help pages (`/help`): the guides in docs/guides, rendered. Public, so a member can read them before
signing in or applying."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from krater.web import help_docs
from krater.web.templates import templates

router = APIRouter()


@router.get("/help", response_class=HTMLResponse)
def help_index(request: Request) -> HTMLResponse:
    guides = help_docs.index()
    return templates.TemplateResponse(
        request,
        "help/index.html",
        {
            "members": [g for g in guides if g.guide.audience == "members"],
            "admins": [g for g in guides if g.guide.audience == "admins"],
        },
    )


@router.get("/help/{slug}", response_class=HTMLResponse)
def help_guide(request: Request, slug: str) -> HTMLResponse:
    rendered = help_docs.render(slug)
    if rendered is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return templates.TemplateResponse(request, "help/guide.html", {"rendered": rendered})
