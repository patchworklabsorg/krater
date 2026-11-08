"""Sharing proposals with funders: `/admin/funders/ospool`, admins only. The page lists every project; the admin
ticks the ones to send and gets them as a PDF (`krater.services.funder_packet` has the rules)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import GalleryMinorNames
from krater.services import funder_packet, site_settings
from krater.services.actor import Actor
from krater.services.errors import FunderPacketUnavailable, ValidationFailed
from krater.weave import WeaveClient, get_weave_client
from krater.web import funder_export
from krater.web.csrf import verify_csrf_token
from krater.web.deps import require_admin
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()

PAGE = "/admin/funders/ospool"


@router.get(PAGE)
def ospool_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
):
    # A page view may use the directory cache; the export asks Weave again, fresh.
    candidates = funder_packet.candidates(db_session, weave_client, actor)
    return templates.TemplateResponse(
        request,
        "admin/funders.html",
        {
            "candidates": candidates,
            "weave_down": any(row.residency is None for row in candidates),
            "names_full": site_settings.current(db_session).gallery_minor_names is GalleryMinorNames.FULL,
            "fields": funder_packet.FIELDS,
        },
    )


@router.post(f"{PAGE}/export", dependencies=[Depends(verify_csrf_token)])
def ospool_export(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    project_id: Annotated[list[uuid.UUID] | None, Form()] = None,
    field: Annotated[list[str] | None, Form()] = None,
):
    try:
        packet = funder_packet.build(db_session, weave_client, actor, project_ids=project_id or [], fields=field or [])
        funder_packet.record_export(db_session, actor, packet)
    except FunderPacketUnavailable as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(PAGE, status_code=303)
    except ValidationFailed as exc:
        db_session.rollback()
        flash(request, " ".join(exc.errors.values()), "error")
        return RedirectResponse(PAGE, status_code=303)
    body = funder_export.to_pdf(packet)
    db_session.commit()
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{funder_export.filename(packet)}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
