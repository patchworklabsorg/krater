"""Small mixins shared by every model: UUID primary keys and server-defaulted timestamps."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """A `uuid.uuid4`-generated primary key, safe to expose in URLs."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    """A timezone-aware `created_at`, defaulted on the server so it's set even on bulk inserts."""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class TimestampMixin(CreatedAtMixin):
    """`created_at` plus a `updated_at` that the server refreshes on every update."""

    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
