"""ImageInspection: what an admin saw when they inspected a container image (`krater.registry.inspect`).

Kept so the admin images page can show an inspection again without asking the registry, and as the record of who
looked at which digest when. Read-only once written.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class ImageInspection(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "image_inspections"

    # The reference inspected, as an admin or a job named it (a tag, or `name@sha256:...`).
    image: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    # The manifest digest it resolved to: the multi-platform index's for one that has several.
    digest: Mapped[str] = mapped_column(sa.String(80), nullable=False, index=True)
    inspected_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    # `krater.registry.ImageDetails.to_json()`.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
