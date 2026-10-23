"""The runner's API: `/runner/v1/...`, called by the runner on a rented machine, never by a browser.

Authenticated by the instance's token in `Authorization: Bearer` (never a query string), with no session and no
CSRF token. Answers JSON. The runner binary itself is public (`GET /runner/v1/krater-runner`): it holds no secret,
and the bootstrap script checks its sha256 before running it. See `krater.compute.runner_api`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from krater.compute import runner_api
from krater.config import Settings, get_settings
from krater.db import get_session
from krater.models import ComputeInstance
from krater.services import compute_settings
from krater.services.errors import NotAllowed, ValidationFailed
from krater.storage import StorageError, get_compute_store

router = APIRouter(prefix="/runner/v1", include_in_schema=False)


def _now() -> datetime:
    return datetime.now(UTC)


def _token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _deny(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=403)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def _instance(request: Request, db_session: Session) -> ComputeInstance:
    return runner_api.authenticate(db_session, _token(request))


@router.get("/krater-runner")
def download_runner(settings: Annotated[Settings, Depends(get_settings)]):
    path = Path(settings.compute_runner_binary_path)
    if not path.is_file():
        return JSONResponse({"error": "The runner isn't built on this server."}, status_code=404)
    return FileResponse(path, media_type="application/octet-stream", filename="krater-runner")


@router.post("/start")
def start(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    try:
        info = runner_api.start(db_session, _instance(request, db_session), now=_now(), settings=settings)
    except NotAllowed as exc:
        db_session.rollback()
        return _deny(exc)
    db_session.commit()
    return {**info.__dict__, "ssh_keys": list(info.ssh_keys)}


@router.post("/heartbeat")
async def heartbeat(request: Request, db_session: Annotated[Session, Depends(get_session)]):
    body = await _body(request)
    try:
        reply = runner_api.heartbeat(db_session, _instance(request, db_session), body, now=_now())
    except NotAllowed as exc:
        db_session.rollback()
        return _deny(exc)
    db_session.commit()
    return {"stop": reply.stop, "sync": reply.sync}


@router.post("/synced")
def synced(request: Request, db_session: Annotated[Session, Depends(get_session)]):
    try:
        runner_api.synced(db_session, _instance(request, db_session), now=_now())
    except NotAllowed as exc:
        db_session.rollback()
        return _deny(exc)
    db_session.commit()
    return {"ok": True}


@router.post("/exit")
async def report_exit(request: Request, db_session: Annotated[Session, Depends(get_session)]):
    body = await _body(request)
    code = body.get("exit_code")
    if not isinstance(code, int) or isinstance(code, bool):
        return JSONResponse({"error": "exit_code must be an integer"}, status_code=422)
    try:
        job = runner_api.report_exit(db_session, _instance(request, db_session), code, now=_now())
    except NotAllowed as exc:
        db_session.rollback()
        return _deny(exc)
    db_session.commit()
    return {"state": job.state.value}


@router.post("/files/{area}/list")
def list_files(area: str, request: Request, db_session: Annotated[Session, Depends(get_session)]):
    return _files(
        request, db_session, lambda instance: {"files": runner_api.list_files(get_compute_store(), instance, area)}
    )


@router.post("/files/{area}/upload")
async def begin_upload(area: str, request: Request, db_session: Annotated[Session, Depends(get_session)]):
    body = await _body(request)
    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool):
        return JSONResponse({"error": "size must be an integer"}, status_code=422)

    def _begin(instance: ComputeInstance) -> dict:
        upload = runner_api.begin_upload(
            db_session,
            get_compute_store(),
            instance,
            area,
            str(body.get("name", "")),
            size,
            quota_bytes=compute_settings.effective_policy(db_session, instance.job.project).storage_quota_gb * 1024**3,
            now=_now(),
        )
        db_session.commit()  # the reservation, and the project's row lock with it
        return upload

    return _files(request, db_session, _begin)


@router.post("/files/{area}/complete")
async def complete_upload(area: str, request: Request, db_session: Annotated[Session, Depends(get_session)]):
    body = await _body(request)
    etags = body.get("etags") if isinstance(body.get("etags"), list) else []

    def _complete(instance: ComputeInstance) -> dict:
        runner_api.complete_upload(
            get_compute_store(), instance, area, str(body.get("name", "")), str(body.get("upload_id", "")), etags
        )
        return {"ok": True}

    return _files(request, db_session, _complete)


def _files(request: Request, db_session: Session, action):
    try:
        return action(_instance(request, db_session))
    except NotAllowed as exc:
        db_session.rollback()
        return _deny(exc)
    except ValidationFailed as exc:
        db_session.rollback()  # lets go of the project's row lock an upload took
        return JSONResponse({"error": str(exc), "fields": exc.errors}, status_code=422)
    except StorageError:
        db_session.rollback()
        return JSONResponse({"error": "Storage is unavailable; try again."}, status_code=503)


__all__ = ["router"]
