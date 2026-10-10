"""Small parsing helpers for form fields that aren't a plain string: comma-separated tags, and
comma-separated emails of existing Krater users (credited builders)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User


def parse_tags(raw: str) -> list[str]:
    """Split a comma-separated tags field into a clean list: trimmed, empties dropped."""
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


class UnknownEmails(ValueError):
    """Raised by `parse_credited_builder_emails` when one or more emails aren't a Krater user's."""

    def __init__(self, emails: list[str]) -> None:
        self.emails = emails
        super().__init__("Unknown email(s): " + ", ".join(emails))


def parse_credited_builder_emails(session: Session, raw: str) -> list[uuid.UUID]:
    """Parse a comma-separated list of emails into the `User.id`s of existing Krater users.

    Raises `UnknownEmails` (naming every email that doesn't match a user) rather than silently
    dropping them -- the caller turns that into a field error.
    """
    emails = [email.strip() for email in raw.split(",") if email.strip()]
    if not emails:
        return []

    users_by_email = {
        user.email.lower(): user
        for user in session.scalars(
            sa.select(User).where(sa.func.lower(User.email).in_([email.lower() for email in emails]))
        )
    }

    unknown = [email for email in emails if email.lower() not in users_by_email]
    if unknown:
        raise UnknownEmails(unknown)

    # Preserve input order, de-duplicated.
    seen: set[uuid.UUID] = set()
    ids: list[uuid.UUID] = []
    for email in emails:
        user_id = users_by_email[email.lower()].id
        if user_id not in seen:
            seen.add(user_id)
            ids.append(user_id)
    return ids


__all__ = ["UnknownEmails", "parse_credited_builder_emails", "parse_tags"]
