"""What the worker last saw of things outside Krater's database, for admins and alerts: the compute provider's account
credit and how much of Krater's storage each project uses. The portal never asks the provider or lists the store
itself; it reads these rows."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base


class ProviderAccount(Base):
    """A provider account's credit as of `observed_at` (`krater.services.compute_watch.record_credit`), one row per
    provider."""

    __tablename__ = "provider_accounts"

    provider: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    # Credit left on the account, in micro-dollars. Negative when the account owes.
    credit_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class StorageUsage(Base):
    """Bytes stored for one scope as of the worker's last count (`krater.services.compute_watch.record_storage`):
    `project:<id>` for a project's checkpoints and outputs, `compute-other` for compute keys of no project, and
    `images` for the gallery's and proposals' images, which share the store."""

    __tablename__ = "storage_usage"

    scope: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    objects: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
