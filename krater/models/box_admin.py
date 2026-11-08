"""BoxAdmin: one row per admin whose SSH keys go on every box, as Weave last said.

The worker's `box_admins_sync` task replaces these rows from Weave's directory every few minutes
(`krater.services.box_access.sync_admins`). The heartbeat reads only this table, so a box's key list never waits on
Weave. A row Weave hasn't confirmed for `krater.services.box_access.ROSTER_STALE` stops counting, so a Weave outage
takes admins' keys off boxes instead of keeping a stale list forever.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base

if TYPE_CHECKING:
    from krater.models.user import User


class BoxAdmin(Base):
    __tablename__ = "box_admins"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True)
    # When Weave last said this user is an admin.
    synced_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:
        return f"<BoxAdmin {self.user_id} synced={self.synced_at}>"
