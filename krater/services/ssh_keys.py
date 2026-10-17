"""Members' SSH public keys, for their Krater Compute boxes.

A key is accepted only as `<type> <base64>[ <comment>]` with one of a few types, and only if the base64 decodes to a
key blob that starts with the same type. Krater keeps just the type and the base64, so nothing stored can add options
(`command=...`) or a second line to a machine's authorized_keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User, UserSshKey
from krater.services.actor import Actor
from krater.services.errors import NotAllowed, NotFound, ValidationFailed

ALLOWED_TYPES = frozenset(
    {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}
)
MAX_KEYS_PER_USER = 10
_MAX_BLOB_BYTES = 8192


@dataclass(frozen=True)
class ParsedKey:
    key_type: str
    key_base64: str
    fingerprint: str
    comment: str


def parse_public_key(text: str) -> ParsedKey:
    line = text.strip()
    if not line or "\n" in line or "\r" in line or "\x00" in line:
        raise ValidationFailed({"key": "Paste one public key, on one line."})
    parts = line.split()
    if len(parts) < 2 or parts[0] not in ALLOWED_TYPES:
        raise ValidationFailed(
            {"key": "Paste a public key that starts with ssh-ed25519, ecdsa-sha2-nistp256/384/521 or ssh-rsa."}
        )
    key_type, key_base64 = parts[0], parts[1]
    try:
        blob = base64.b64decode(key_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationFailed({"key": "That key's base64 part isn't valid."}) from None
    if not 4 < len(blob) <= _MAX_BLOB_BYTES:
        raise ValidationFailed({"key": "That doesn't look like a public key."})
    name_length = int.from_bytes(blob[:4], "big")
    if blob[4 : 4 + name_length] != key_type.encode():
        raise ValidationFailed({"key": "The key's type doesn't match its contents."})
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return ParsedKey(key_type, key_base64, f"SHA256:{digest}", " ".join(parts[2:])[:100])


def list_keys(session: Session, user: User) -> list[UserSshKey]:
    return list(
        session.scalars(sa.select(UserSshKey).where(UserSshKey.user_id == user.id).order_by(UserSshKey.created_at))
    )


def _default_name(parsed: ParsedKey) -> str:
    """A name for a key added without one: its comment, unless that is an email (`ssh-keygen` writes `user@host`),
    since Krater stores no member emails (docs/weave-integration.md)."""
    return parsed.comment if parsed.comment and "@" not in parsed.comment else parsed.key_type


def add_key(session: Session, actor: Actor, *, name: str, text: str) -> UserSshKey:
    """Add a key to the actor's own account."""
    if not actor.is_member:
        raise NotAllowed("Only Ganymede members can add SSH keys.")
    parsed = parse_public_key(text)
    existing = list_keys(session, actor.user)
    if len(existing) >= MAX_KEYS_PER_USER:
        raise ValidationFailed({"key": f"You can have up to {MAX_KEYS_PER_USER} keys. Remove one first."})
    if any(k.fingerprint == parsed.fingerprint for k in existing):
        raise ValidationFailed({"key": "That key is already on your account."})
    key = UserSshKey(
        user_id=actor.user.id,
        name=(name.strip() or _default_name(parsed))[:100],
        key_type=parsed.key_type,
        key_base64=parsed.key_base64,
        fingerprint=parsed.fingerprint,
    )
    session.add(key)
    session.flush()
    return key


def remove_key(session: Session, actor: Actor, *, key_id: uuid.UUID) -> None:
    key = session.get(UserSshKey, key_id)
    if key is None or key.user_id != actor.user.id:
        raise NotFound("No such key on your account.")
    session.delete(key)
    session.flush()


def authorized_keys_for(session: Session, user_id: uuid.UUID) -> list[str]:
    """The lines to put in a box's authorized_keys for its submitter."""
    return [
        key.authorized_keys_line
        for key in session.scalars(
            sa.select(UserSshKey).where(UserSshKey.user_id == user_id).order_by(UserSshKey.created_at)
        )
    ]
