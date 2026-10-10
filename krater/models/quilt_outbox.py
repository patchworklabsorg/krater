"""QuiltOutbox: one event for Quilt's patch API, written in the same transaction as the change it reports.

The row's `id` is the event id Quilt sees, so a retry always sends the same id and payload. `seq` gives
the order: the sender sends the rows of one subject (`external_id`) strictly in `seq` order. See
`docs/quilt-integration.md`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.enums import QuiltOutboxState, pg_enum
from krater.models.mixins import CreatedAtMixin


class QuiltOutbox(CreatedAtMixin, Base):
    __tablename__ = "quilt_outbox"
    __table_args__ = (
        sa.Index("ix_quilt_outbox_external_id_seq", "external_id", "seq"),
        sa.Index("ix_quilt_outbox_state_seq", "state", "seq"),
    )

    # The event id sent to Quilt. Derived with uuid5 from the source rows, never random: see
    # `krater.services.quilt_events`.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    # The event's `data` object, exactly as it is sent.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    state: Mapped[QuiltOutboxState] = mapped_column(
        pg_enum(QuiltOutboxState, name="quilt_outbox_state"),
        nullable=False,
        default=QuiltOutboxState.PENDING,
        server_default=QuiltOutboxState.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    # `None` means "send it on the next run". Set after a failed attempt, for the backoff.
    next_attempt_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The HTTP status of the last attempt (`None`: no attempt yet, or no answer).
    last_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    def event_body(self) -> dict:
        """The JSON body for `POST /api/v1/events`."""
        return {
            "id": str(self.id),
            "type": self.type,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "data": self.payload,
        }

    def __repr__(self) -> str:
        return f"<QuiltOutbox {self.type} external_id={self.external_id} state={self.state.value}>"
