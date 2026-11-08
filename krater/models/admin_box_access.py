"""AdminBoxAccess: an admin's time-limited SSH access to one running box, granted with a reason (until 2026-11-08).

History only: since then every admin's keys are on every box (`BoxAdmin`, `krater.services.box_access`), and nothing
writes these rows. They're kept as the record of who had access and why; the grants and revocations also wrote
`AuditEvent`s (`admin_box_access`, `admin_box_access_revoked`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.compute_job import ComputeJob
    from krater.models.user import User


class AdminBoxAccess(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "admin_box_access"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("compute_jobs.id"), nullable=False, index=True
    )
    admin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    revoked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )

    job: Mapped[ComputeJob] = relationship()
    admin: Mapped[User] = relationship(foreign_keys=[admin_id])

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at

    def __repr__(self) -> str:
        return f"<AdminBoxAccess job={self.job_id} admin={self.admin_id} until={self.expires_at}>"
