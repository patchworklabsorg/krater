"""ImageFact: what an image's registry said about one digest (`krater.registry.inspect`), kept for the checks Krater
runs on a job's image when it's queued (`krater.services.image_checks`). A digest's content never changes, so a row
is never updated; it only saves asking the registry again (Docker Hub counts manifest GETs against its pull limit).
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base


class ImageFact(Base):
    __tablename__ = "image_facts"

    # The manifest digest the image's tag resolved to (the multi-platform index's for one that has several).
    digest: Mapped[str] = mapped_column(sa.String(80), primary_key=True)
    # `krater.registry.ImageDetails.to_json()`, for the linux/amd64 image.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
