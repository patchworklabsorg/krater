"""The guardian check: the safeguard for team members under 18 (docs/SPEC.md "Verification before compute").

Weave asks a parent or guardian to consent by email for a member under 18. An email link proves control of an
inbox, not parenthood. So a Weave admin also records a stronger check in Weave: a signed guardian form plus a
short call or video check with the guardian. Weave's directory then says `guardian_verified: true`. Krater
stores none of this.

A member under 18 without a verified guardian isn't cleared to use a project's compute. The gate that applies this
together with ID verification is `krater.services.clearance`; this module keeps the team and the reason texts.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import Project, User
from krater.services import teams

#: Why a team member isn't cleared, as shown on the project page.
REASON_NO_VERIFIED_GUARDIAN = "under 18 without a verified guardian in Weave"
REASON_NOT_IN_WEAVE = "not available in Weave (no access to Krater)"


@dataclass(frozen=True)
class PendingMember:
    """A team member Weave hasn't cleared (`krater.services.clearance`), and why: one reason or several, joined."""

    user: User
    name: str
    reason: str = REASON_NO_VERIFIED_GUARDIAN

    @property
    def label(self) -> str:
        """The member's name and Patchwork ID, for messages to reviewers and admins."""
        sub = self.user.weave_sub
        return f"{self.name} ({sub})" if self.name and self.name != sub else sub


def team_users(session: Session, project: Project) -> list[User]:
    """The project's team as it is now: the lead, then accepted members in the order they were invited."""
    ids = teams.team_user_ids(project)
    by_id = {user.id: user for user in session.scalars(sa.select(User).where(User.id.in_(ids)))}
    return [by_id[uid] for uid in dict.fromkeys(ids) if uid in by_id]


__all__ = [
    "REASON_NOT_IN_WEAVE",
    "REASON_NO_VERIFIED_GUARDIAN",
    "PendingMember",
    "team_users",
]
