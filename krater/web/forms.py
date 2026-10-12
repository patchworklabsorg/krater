"""Small parsing helpers for form fields that aren't a plain string: comma-separated tags, and
comma-separated Patchwork IDs or emails of Ganymede members (credited builders)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from krater.services import people
from krater.weave import WeaveClient


def parse_tags(raw: str) -> list[str]:
    """Split a comma-separated tags field into a clean list: trimmed, empties dropped."""
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


class UnknownMembers(ValueError):
    """Raised by `parse_members` when one or more entries aren't a Ganymede member Weave knows."""

    def __init__(self, entries: list[str]) -> None:
        self.entries = entries
        super().__init__("Not a Ganymede member: " + ", ".join(entries))


def parse_members(session: Session, weave_client: WeaveClient, raw: str) -> list[uuid.UUID]:
    """Parse comma-separated Patchwork IDs or emails into the `User.id`s of Ganymede members, looked up in
    Weave (`krater.services.people.find_member`). A member who never signed in to Krater gets a user row
    holding only their Patchwork ID.

    Raises `UnknownMembers` naming every entry Weave doesn't match, rather than silently dropping them;
    `WeaveUnavailableError` propagates.
    """
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    ids: list[uuid.UUID] = []
    unknown: list[str] = []
    for entry in entries:
        record = people.find_member(weave_client, entry)
        if record is None:
            unknown.append(entry)
            continue
        user_id = people.user_for(session, record).id
        if user_id not in ids:
            ids.append(user_id)
    if unknown:
        raise UnknownMembers(unknown)
    return ids


__all__ = ["UnknownMembers", "parse_members", "parse_tags"]
