"""Project secrets: values a project's jobs need (a Hugging Face token, say), stored encrypted and delivered by the
runner.

A secret's value is write-only: the team sets or replaces it and can see its name, never its value again. Krater
encrypts it with the keys in `KRATER_PROJECT_SECRETS_KEY_FILE` (Fernet, newest key first, so old values still read
after a rotation; `python -m krater.rotate_project_secrets` then re-encrypts them all with the newest, so the old
key can go). The runner fetches the project's secrets with its own token when it starts
(`krater.compute.runner_api.start`), so values never go into Vast's create request. The machine's host can still read
them, like anything on that machine, so members should use tokens they can scope and revoke.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import Project, ProjectSecret
from krater.services import audit, teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed

logger = logging.getLogger(__name__)

MAX_SECRETS_PER_PROJECT = 20
MAX_VALUE_BYTES = 8192
NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
#: Names Krater, Vast, the runner or the shell already use: a secret must not shadow them.
RESERVED_PREFIXES = ("KRATER_", "VAST_", "CONTAINER_", "LD_", "PYTHON", "NVIDIA_", "CUDA_")
RESERVED_NAMES = frozenset(
    {"PATH", "HOME", "USER", "SHELL", "PWD", "HOSTNAME", "TERM", "LANG", "PUBLIC_IPADDR", "JUPYTER_TOKEN"}
)


def _keys(settings: Settings) -> list[str]:
    if settings.project_secrets_key_file:
        try:
            text = Path(settings.project_secrets_key_file).read_text(encoding="utf-8")
        except OSError:
            logger.warning("krater.project_secrets: could not read KRATER_PROJECT_SECRETS_KEY_FILE")
            return []
        return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return [k.strip() for k in settings.project_secrets_key.split(",") if k.strip()]


def cipher(settings: Settings) -> MultiFernet | None:
    """The cipher for project secrets, or `None` when no key is set (the feature is off)."""
    keys = _keys(settings)
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except ValueError:
        logger.error("krater.project_secrets: a project secrets key isn't a valid Fernet key")
        return None


def enabled(settings: Settings) -> bool:
    return cipher(settings) is not None


def _require_cipher(settings: Settings) -> MultiFernet:
    c = cipher(settings)
    if c is None:
        raise InvalidState("Project secrets aren't set up on this Krater: an admin needs to add a key first.")
    return c


def _can_manage(actor: Actor, project: Project) -> bool:
    is_team = teams.on_team(project, actor.user.id)
    return actor.is_member and (is_team or actor.is_admin)


def list_secrets(session: Session, project: Project) -> list[ProjectSecret]:
    """A project's secrets, by name. Only names and dates are for showing."""
    return list(
        session.scalars(
            sa.select(ProjectSecret).where(ProjectSecret.project_id == project.id).order_by(ProjectSecret.name)
        )
    )


def validate_name(name: str) -> str:
    name = name.strip()
    if not NAME_RE.match(name):
        raise ValidationFailed(
            {"name": "Use capital letters, digits and underscores, starting with a letter or underscore (HF_TOKEN)."}
        )
    if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES):
        raise ValidationFailed({"name": f"{name} is used by Krater, Vast or the system. Choose another name."})
    return name


def set_secret(
    session: Session, actor: Actor, *, project: Project, name: str, value: str, settings: Settings
) -> ProjectSecret:
    """Add a secret or replace its value. The team or an admin. Flushes; the caller commits."""
    if not _can_manage(actor, project):
        raise NotAllowed("Only the project's team or an admin can change its secrets.")
    name = validate_name(name)
    if not value:
        raise ValidationFailed({"value": "Enter the secret's value."})
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise ValidationFailed({"value": f"A secret can be up to {MAX_VALUE_BYTES} bytes."})
    if "\x00" in value:
        raise ValidationFailed({"value": "A secret can't contain a NUL character."})
    ciphertext = _require_cipher(settings).encrypt(value.encode()).decode()
    existing = session.scalar(
        sa.select(ProjectSecret).where(ProjectSecret.project_id == project.id, ProjectSecret.name == name)
    )
    if existing is None:
        count = session.scalar(sa.select(sa.func.count()).where(ProjectSecret.project_id == project.id))
        if count >= MAX_SECRETS_PER_PROJECT:
            raise ValidationFailed({"name": f"A project can have up to {MAX_SECRETS_PER_PROJECT} secrets."})
        existing = ProjectSecret(project_id=project.id, name=name, ciphertext=ciphertext, updated_by_id=actor.user.id)
        session.add(existing)
        action = "project_secret.added"
    else:
        existing.ciphertext = ciphertext
        existing.updated_by_id = actor.user.id
        action = "project_secret.replaced"
    session.flush()
    audit.record(session, actor, action, project=project, payload={"name": name})  # the name only, never the value
    return existing


def delete_secret(session: Session, actor: Actor, *, project: Project, name: str) -> None:
    if not _can_manage(actor, project):
        raise NotAllowed("Only the project's team or an admin can change its secrets.")
    secret = session.scalar(
        sa.select(ProjectSecret).where(ProjectSecret.project_id == project.id, ProjectSecret.name == name)
    )
    if secret is None:
        raise NotFound(f"No secret named {name}.")
    session.delete(secret)
    session.flush()
    audit.record(session, actor, "project_secret.deleted", project=project, payload={"name": name})


def rotate_all(session: Session, settings: Settings) -> tuple[int, list[str]]:
    """Re-encrypt every stored secret with the newest key (the first line of the key file), so an older key can then
    be removed. Returns how many were re-encrypted and the `project_id/NAME` of any no key could read. Flushes."""
    c = _require_cipher(settings)
    done, unreadable = 0, []
    for row in session.scalars(sa.select(ProjectSecret)):
        try:
            row.ciphertext = c.rotate(row.ciphertext.encode()).decode()
            done += 1
        except InvalidToken:
            unreadable.append(f"{row.project_id}/{row.name}")
    session.flush()
    return done, unreadable


def environment(session: Session, project_id, settings: Settings) -> dict[str, str]:
    """A project's secrets as name -> value, for its runner. A value that can't be decrypted (its key was removed)
    is left out and logged by name."""
    rows = list(session.scalars(sa.select(ProjectSecret).where(ProjectSecret.project_id == project_id)))
    if not rows:
        return {}
    c = cipher(settings)
    if c is None:
        logger.error("krater.project_secrets: project %s has secrets but no key is set; sending none", project_id)
        return {}
    env: dict[str, str] = {}
    for row in rows:
        try:
            env[row.name] = c.decrypt(row.ciphertext.encode()).decode()
        except InvalidToken:
            logger.error("krater.project_secrets: can't decrypt %s for project %s; left out", row.name, project_id)
    return env
