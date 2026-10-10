"""User: a cache of the Weave identity. Roles live in Weave; `roles_cached` is for display only (see
CLAUDE.md)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.audit_event import AuditEvent
    from krater.models.budget_entry import BudgetEntry
    from krater.models.project import Project
    from krater.models.review import Review


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    weave_sub: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # Whether Weave said `email` was verified, as of the latest sign-in or directory lookup. The Slack
    # email lookup only trusts a verified email.
    email_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    # Weave's `slack_id`, else found by a Slack email lookup. Only maps a Slack click to a Krater user;
    # the click is then re-checked against Weave by `weave_sub`. Unique so a click maps to one user.
    slack_user_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True, nullable=True)
    # Krater role names (`ganymede:*`) Weave last reported, at sign-in or a directory lookup. Display and
    # navigation only: every action re-checks Weave (see `krater.web.deps.fresh_actor`).
    roles_cached: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False, default=list, server_default="{}")
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    projects: Mapped[list[Project]] = relationship(foreign_keys="Project.submitter_id", back_populates="submitter")
    reviews: Mapped[list[Review]] = relationship(back_populates="reviewer")
    budget_entries: Mapped[list[BudgetEntry]] = relationship(back_populates="actor")
    audit_events: Mapped[list[AuditEvent]] = relationship(back_populates="actor")

    def __repr__(self) -> str:
        return f"<User {self.weave_sub} {self.display_name!r}>"
