"""SlackNotification: tracks which `AuditEvent`s the Slack reconciler has already posted about.

Only used for events the periodic `slack_reconcile` job discovers on its own (`budget_warning` /
`budget_teardown`, written by `krater.services.skypilot_sync`) rather than ones posted immediately by
the action that caused them -- see `krater.services.slack_notify.sync_budget_notifications`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.audit_event import AuditEvent


class SlackNotification(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "slack_notifications"

    audit_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("audit_events.id"), nullable=False, unique=True, index=True
    )

    audit_event: Mapped[AuditEvent] = relationship()

    def __repr__(self) -> str:
        return f"<SlackNotification audit_event_id={self.audit_event_id}>"
