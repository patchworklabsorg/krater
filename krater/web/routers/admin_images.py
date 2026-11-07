"""Admin review of the container images members run: `/admin/images` (every image named by a job, with the digests
Krater rented machines with) and inspecting one (`krater.registry.inspect`: its config and build history, kept as an
`ImageInspection`). Admins only. Inspecting asks the image's registry, so it is a POST, checked in Weave."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import ComputeInstance, ComputeJob, ImageInspection, Project, User
from krater.registry import ImageDetails, Registry, RegistryError, get_registry, parse_reference
from krater.services import people
from krater.services.actor import Actor
from krater.services.errors import NotFound
from krater.weave import WeaveClient, get_weave_client
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor, require_admin
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()


def _pinned(image: str, digest: str) -> str | None:
    try:
        return parse_reference(image).pinned(digest)
    except (RegistryError, ValueError):
        return None


def _latest_inspections(db_session: Session, digests: set[str]) -> dict[str, ImageInspection]:
    if not digests:
        return {}
    rows = db_session.scalars(
        sa.select(ImageInspection)
        .where(ImageInspection.digest.in_(digests))
        .order_by(ImageInspection.created_at.desc())
    )
    latest: dict[str, ImageInspection] = {}
    for row in rows:
        latest.setdefault(row.digest, row)
    return latest


@router.get("/admin/images")
def images_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    images = db_session.execute(
        sa.select(
            ComputeJob.image,
            sa.func.count(ComputeJob.id),
            sa.func.count(sa.distinct(ComputeJob.project_id)),
            sa.func.max(ComputeJob.queued_at),
        )
        .group_by(ComputeJob.image)
        .order_by(sa.func.max(ComputeJob.queued_at).desc())
    ).all()
    digest_rows = db_session.execute(
        sa.select(
            ComputeJob.image,
            ComputeInstance.image_digest,
            sa.func.count(ComputeInstance.id),
            sa.func.bool_or(ComputeInstance.image_pinned),
            sa.func.max(ComputeInstance.created_at),
        )
        .join(ComputeJob, ComputeInstance.job_id == ComputeJob.id)
        .where(ComputeInstance.image_digest.is_not(None))
        .group_by(ComputeJob.image, ComputeInstance.image_digest)
        .order_by(sa.func.max(ComputeInstance.created_at).desc())
    ).all()
    latest = _latest_inspections(db_session, {row[1] for row in digest_rows})
    digests: dict[str, list[dict]] = {}
    for image, digest, machines, pinned, last in digest_rows:
        digests.setdefault(image, []).append(
            {
                "digest": digest,
                "machines": machines,
                "pinned": pinned,
                "last": last,
                "reference": _pinned(image, digest),
                "inspection": latest.get(digest),
            }
        )
    return templates.TemplateResponse(
        request,
        "admin/images.html",
        {
            "images": [
                {"image": image, "jobs": jobs, "projects": projects, "last": last, "digests": digests.get(image, [])}
                for image, jobs, projects, last in images
            ],
        },
    )


@router.post("/admin/images/inspect", dependencies=[Depends(verify_csrf_token)])
def inspect_image(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    registry: Annotated[Registry, Depends(get_registry)],
    image: Annotated[str, Form()] = "",
):
    if not actor.is_admin:
        raise NotFound("Not found.")
    image = image.strip()
    try:
        details = registry.inspect(image)
    except RegistryError as exc:
        flash(request, f"Couldn't inspect {image}: {exc}", "error")
        return RedirectResponse("/admin/images", status_code=303)
    row = ImageInspection(image=image, digest=details.digest, inspected_by_id=actor.user.id, details=details.to_json())
    db_session.add(row)
    db_session.commit()
    return RedirectResponse(f"/admin/images/inspections/{row.id}", status_code=303)


@router.get("/admin/images/inspections/{inspection_id}")
def inspection_page(
    request: Request,
    inspection_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    row = db_session.get(ImageInspection, inspection_id)
    if row is None:
        raise NotFound(f"No inspection {inspection_id}.")
    details = ImageDetails.from_json(row.details)
    digests = {details.digest} | ({details.platform_digest} if details.platform_digest else set())
    ran = db_session.execute(
        sa.select(ComputeJob, Project, sa.func.count(ComputeInstance.id), sa.func.bool_or(ComputeInstance.image_pinned))
        .join(ComputeInstance, ComputeInstance.job_id == ComputeJob.id)
        .join(Project, Project.id == ComputeJob.project_id)
        .where(ComputeInstance.image_digest.in_(digests))
        .group_by(ComputeJob.id, Project.id)
        .order_by(sa.func.max(ComputeInstance.created_at).desc())
        .limit(100)
    ).all()
    inspector = db_session.get(User, row.inspected_by_id)
    return templates.TemplateResponse(
        request,
        "admin/image_inspection.html",
        {
            "row": row,
            "details": details,
            "ran": [
                {"job": job, "project": project, "machines": machines, "pinned": pinned}
                for job, project, machines, pinned in ran
            ],
            "inspector": people.names(weave_client, [inspector])[inspector.id] if inspector else "",
        },
    )
