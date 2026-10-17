"""Site-wide settings (`SiteSettings`, one row): read anywhere, changed by an admin with a reason and an audit event."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import GalleryMinorNames, SiteSettings
from krater.models.site_settings import SITE_ID
from krater.services import audit
from krater.services.actor import Actor
from krater.services.errors import NotAllowed, ValidationFailed

AUDIT_UPDATE = "site_settings_update"

#: What admins choose between, as the admin page words it.
GALLERY_MINOR_NAMES_LABELS = {
    GalleryMinorNames.FULL: "Full name",
    GalleryMinorNames.FIRST: "First name only",
    GalleryMinorNames.HIDDEN: "Not named",
}


def current(session: Session) -> SiteSettings:
    row = session.get(SiteSettings, SITE_ID)
    if row is None:
        # The migration seeds it; recreate it with the defaults if it was ever deleted.
        row = SiteSettings(id=SITE_ID, gallery_minor_names=GalleryMinorNames.FIRST)
        session.add(row)
        session.flush()
    return row


def set_gallery_minor_names(session: Session, actor: Actor, *, value: str, reason: str) -> None:
    """How the public gallery names team members under 18. Admin only; `reason` required."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may change site settings.")
    try:
        choice = GalleryMinorNames(value)
    except ValueError:
        raise ValidationFailed({"gallery_minor_names": "Choose how the gallery names members under 18."}) from None
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})
    row = current(session)
    if row.gallery_minor_names is choice:
        return
    audit.record(
        session,
        actor,
        AUDIT_UPDATE,
        payload={"changed": {"gallery_minor_names": {"old": row.gallery_minor_names.value, "new": choice.value}}},
        reason=reason.strip(),
    )
    row.gallery_minor_names = choice
    row.updated_by_id = actor.user.id
    session.flush()
