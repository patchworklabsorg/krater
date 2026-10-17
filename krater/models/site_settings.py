"""Site-wide settings that aren't compute policy: one row (`id = 1`), seeded by its migration. Admins change them
on `/admin`; `krater.services.site_settings` owns the changes, and every change writes an `AuditEvent`."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.enums import GalleryMinorNames, pg_enum

#: The one `SiteSettings` row's id.
SITE_ID = 1


class SiteSettings(Base):
    __tablename__ = "site_settings"
    __table_args__ = (sa.CheckConstraint(f"id = {SITE_ID}", name="singleton"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, default=SITE_ID)
    # How the public gallery names team members under 18 (or of unknown age).
    gallery_minor_names: Mapped[GalleryMinorNames] = mapped_column(
        pg_enum(GalleryMinorNames, name="gallery_minor_names"),
        nullable=False,
        default=GalleryMinorNames.FIRST,
        server_default=GalleryMinorNames.FIRST.value,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )
