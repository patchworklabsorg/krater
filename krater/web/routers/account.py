"""The signed-in member's own settings: `/account/ssh-keys` (SSH public keys for their Krater Compute boxes)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.services import ssh_keys
from krater.services.actor import Actor
from krater.services.errors import NotAllowed, NotFound, ValidationFailed
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor, session_actor
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()


def _page(request: Request, db_session: Session, actor: Actor, *, errors=None, posted=None, status_code=200):
    return templates.TemplateResponse(
        request,
        "account/ssh_keys.html",
        {
            "keys": ssh_keys.list_keys(db_session, actor.user),
            "errors": errors or {},
            "posted": posted or {},
            "max_keys": ssh_keys.MAX_KEYS_PER_USER,
        },
        status_code=status_code,
    )


@router.get("/account/ssh-keys")
def ssh_keys_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(session_actor)],
):
    return _page(request, db_session, actor)


@router.post("/account/ssh-keys", dependencies=[Depends(verify_csrf_token)])
def add_ssh_key(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    name: Annotated[str, Form()] = "",
    key: Annotated[str, Form()] = "",
):
    try:
        ssh_keys.add_key(db_session, actor, name=name, text=key)
    except ValidationFailed as exc:
        db_session.rollback()
        return _page(request, db_session, actor, errors=exc.errors, posted={"name": name, "key": key}, status_code=422)
    except NotAllowed:
        db_session.rollback()
        raise
    db_session.commit()
    flash(request, "SSH key added. It goes into your boxes the next time they start.", "success")
    return RedirectResponse("/account/ssh-keys", status_code=303)


@router.post("/account/ssh-keys/{key_id}/delete", dependencies=[Depends(verify_csrf_token)])
def remove_ssh_key(
    request: Request,
    key_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    try:
        ssh_keys.remove_key(db_session, actor, key_id=key_id)
    except NotFound:
        db_session.rollback()
        raise
    db_session.commit()
    flash(request, "SSH key removed. Boxes started from now on won't accept it.", "success")
    return RedirectResponse("/account/ssh-keys", status_code=303)


__all__ = ["router"]
