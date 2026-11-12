"""The guardian check: the safeguard for team members under 18 (docs/SPEC.md "Verification before funding").

Weave asks a parent or guardian to consent by email for a member under 18, and won't let a member under 18 use
Krater without that consent. Weave's ID verification then needs the guardian too: it passes a member under 18 only
with a granted guardian consent on file and the parent or guardian on the verification call (Weave's docs/KYC.md
"Pass rules"). So for a minor, `verification_status: "verified"` already means the stronger guardian check: a
signed consent and a live call with the guardian. Krater stores none of this.

Weave's directory also has `guardian_verified`, which until now only a separate Weave admin step ("Record a verified
guardian") set, never the verification call. Krater doesn't wait for it: a member under 18 whose ID verification
passed is cleared. The gate is `krater.services.clearance`; this module keeps the team and the reason texts.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import Project, User
from krater.services import teams

#: Added to the reason of a member under 18 who hasn't passed ID verification yet, as shown on the project page.
MINOR_NOTE = "under 18, so a parent or guardian consents in Weave and joins the call"
REASON_NOT_IN_WEAVE = "not available in Weave (no access to Krater)"


@dataclass(frozen=True)
class PendingMember:
    """One team member and whether Weave clears them (`krater.services.clearance`): `reason` says why not (one
    reason or several, joined), and is empty when they're cleared."""

    user: User
    name: str
    reason: str = ""

    @property
    def cleared(self) -> bool:
        return not self.reason

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
    "MINOR_NOTE",
    "REASON_NOT_IN_WEAVE",
    "PendingMember",
    "team_users",
]
