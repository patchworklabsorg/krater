"""Space a runner's upload has been promised but may not have used yet (`krater.compute.runner_api.begin_upload`)."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base


class StorageReservation(Base):
    """An upload Krater signed URLs for, until they expire. Storage lists a file only once its upload finishes, so
    without these two machines (or one machine's parallel uploads) could each be told the quota has room for them."""

    __tablename__ = "storage_reservations"

    key: Mapped[str] = mapped_column(sa.String(1024), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
