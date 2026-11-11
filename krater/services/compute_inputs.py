"""A project's inputs: files its team uploads once (`krater inputs upload`) for jobs to use.

They live in the project's storage at `projects/{id}/inputs/<name>` and count toward its storage quota. A job names
the ones it wants (`--input data/train.tar`, or `--input data/` for a folder); its runner copies those into
`$KRATER_INPUT_DIR` before the command starts, and the job fails to start rather than run without them. Their size,
at queue time, counts as data into the machine in the transfer estimate and when choosing a host. The download itself
is billed as it happens, through the runner's network count (`docs/compute/runner.md`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from krater.compute import runner_api
from krater.models import Project
from krater.services import compute_settings, teams
from krater.services.actor import Actor
from krater.services.errors import NotAllowed, ValidationFailed
from krater.storage import ObjectStore

#: The most names one job can ask for.
MAX_NAMES = 50


@dataclass(frozen=True)
class InputFile:
    name: str
    size_bytes: int


def _team_only(actor: Actor, project: Project) -> None:
    if not (actor.is_member and teams.on_team(project, actor.user.id)):
        raise NotAllowed("Only the project's team can change its inputs.")


def list_inputs(store: ObjectStore, project_id: uuid.UUID) -> list[InputFile]:
    prefix = runner_api.inputs_prefix(project_id)
    files = [InputFile(item.key[len(prefix) :], item.size_bytes) for item in store.list_prefix(prefix)]
    return sorted(files, key=lambda f: f.name)


def check_names(names: list[str] | tuple[str, ...]) -> list[str]:
    """The names a job asks for, checked (each a stored name, or a folder ending in /). Raises `ValidationFailed`."""
    if len(names) > MAX_NAMES:
        raise ValidationFailed({"inputs": f"Ask for at most {MAX_NAMES} inputs; name a folder (ending in /) instead."})
    checked = []
    for name in names:
        try:
            runner_api.check_name(name.removesuffix("/"))
        except ValidationFailed:
            raise ValidationFailed({"inputs": f"'{name}' isn't a usable input name."}) from None
        if name not in checked:
            checked.append(name)
    return checked


def resolve(store: ObjectStore, project_id: uuid.UUID, names: list[str] | tuple[str, ...]) -> int:
    """The total size of the stored inputs `names` picks. Raises `ValidationFailed` for a name that matches nothing,
    so a typo fails at submit and not on a rented machine."""
    names = check_names(names)
    if not names:
        return 0
    files = {f.name: f.size_bytes for f in list_inputs(store, project_id)}
    missing = [n for n in names if not runner_api.selected(list(files), [n])]
    if missing:
        raise ValidationFailed(
            {"inputs": f"No input named {', '.join(missing)}. `krater inputs list` shows the project's inputs."}
        )
    return sum(files[n] for n in runner_api.selected(list(files), names))


def begin_upload(
    session: Session, store: ObjectStore, actor: Actor, *, project: Project, name: str, size: int, now: datetime
) -> dict[str, Any]:
    """Presigned URL(s) for uploading input `name` of `size` bytes, within the project's storage quota (as a machine's
    uploads are). The caller commits."""
    _team_only(actor, project)
    key = runner_api.inputs_prefix(project.id) + runner_api.check_name(name)
    quota = compute_settings.effective_policy(session, project).storage_quota_gb * 1024**3
    return runner_api.reserve_upload(session, store, project.id, key, name, size, quota_bytes=quota, now=now)


def complete_upload(
    store: ObjectStore, actor: Actor, *, project: Project, name: str, upload_id: str, etags: list[str]
) -> None:
    _team_only(actor, project)
    key = runner_api.inputs_prefix(project.id) + runner_api.check_name(name)
    runner_api.finish_upload(store, key, upload_id, etags)


def delete_input(store: ObjectStore, actor: Actor, *, project: Project, name: str) -> int:
    """Delete input `name`, or everything under it when it ends in /. Returns how many files went. Jobs already
    queued that asked for it fail to start."""
    _team_only(actor, project)
    prefix = runner_api.inputs_prefix(project.id)
    stored = [f.name for f in list_inputs(store, project.id)]
    gone = runner_api.selected(stored, [name])
    if not gone:
        raise ValidationFailed({"name": f"No input named {name}."})
    for each in gone:
        store.delete(prefix + each)
    return len(gone)
