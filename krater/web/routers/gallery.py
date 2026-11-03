"""The public gallery of completed projects: `/gallery`, `/gallery/{id}`. No auth -- anything not
`completed` 404s, so an unfinished project's details never leak here."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import Project, ProjectRevision, ProjectStatus, User
from krater.services import budget, people, site_settings, teams
from krater.services import projects as project_service
from krater.services.errors import NotFound
from krater.storage import ObjectStore, get_object_store
from krater.weave import WeaveClient, get_weave_client
from krater.web.templates import templates

router = APIRouter()


def primary_image_url(store: ObjectStore, revision: ProjectRevision | None) -> str | None:
    """A short-lived presigned GET URL for `revision`'s primary image, if it has one. The gallery only calls this
    for a `completed` project's revision, per docs/SPEC.md: "only objects from approved completion revisions are
    served publicly" (the completion form's preview calls it for the team's own draft)."""
    if revision is None or not revision.screenshot_keys:
        return None
    return store.presign_download(revision.screenshot_keys[0])


def public_team(db_session: Session, weave_client: WeaveClient, project: Project) -> tuple[list[str], int]:
    """How the gallery names `project`'s team: `krater.services.people.public_names`."""
    team_ids = teams.team_user_ids(project)
    by_id = {user.id: user for user in db_session.scalars(sa.select(User).where(User.id.in_(team_ids)))}
    return people.public_names(
        weave_client,
        [by_id[user_id] for user_id in team_ids if user_id in by_id],
        minors=site_settings.current(db_session).gallery_minor_names,
    )


@router.get("/gallery")
def gallery_index(request: Request, db_session: Annotated[Session, Depends(get_session)]):
    completed = list(
        db_session.scalars(
            sa.select(Project).where(Project.status == ProjectStatus.COMPLETED).order_by(Project.updated_at.desc())
        )
    )
    store = get_object_store()
    entries = [
        {
            "project": project,
            "revision": project.current_revision,
            "spend_cents": budget.latest_spend_cents(db_session, project),
            "thumbnail_url": primary_image_url(store, project.current_revision),
        }
        for project in completed
    ]
    return templates.TemplateResponse(request, "gallery/index.html", {"entries": entries})


@router.get("/gallery/{project_id}")
def gallery_detail(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    project = project_service.get_project(db_session, project_id=project_id)
    if project.status is not ProjectStatus.COMPLETED:
        raise NotFound(f"No completed project with id {project_id}.")

    # A completed project's `current_revision` is its approved completion revision: approving a
    # completion moves the project straight to `completed` without opening another draft.
    revision = project.current_revision
    team, unnamed = public_team(db_session, weave_client, project)
    return templates.TemplateResponse(
        request,
        "gallery/detail.html",
        {
            "project": project,
            "revision": revision,
            "team": team,
            "unnamed": unnamed,
            "spend_cents": budget.latest_spend_cents(db_session, project),
            "image_url": primary_image_url(get_object_store(), revision),
        },
    )


__all__ = ["primary_image_url", "public_team", "router"]
