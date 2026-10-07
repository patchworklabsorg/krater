"""Who is performing an action, and which Krater roles Weave says they hold.

Built by the web layer (or Slack handlers) from a fresh Weave directory lookup (see
`krater.services.users.authorize`), then passed explicitly into services. Services authorize against
`groups` here and never read `User.roles_cached`. The role names are Krater's own `ganymede:*` names;
`krater.weave.roles` translates Weave's role keys (or group slugs) into them, so review snapshots
(`Review.reviewer_groups`) and approval policies (`ApprovalPolicy.required_group`) read the same.
"""

from __future__ import annotations

from dataclasses import dataclass

from krater.models import User

GROUP_MEMBER = "ganymede:member"
GROUP_REVIEWER = "ganymede:reviewer"
GROUP_ADMIN = "ganymede:admin"


@dataclass(frozen=True)
class Actor:
    user: User
    groups: frozenset[str]
    # Weave's `slack_member` from the same lookup; `None` when Weave didn't say (or for an actor not
    # built from a fresh lookup). The Slack membership gate prefers it over asking Slack.
    slack_member: bool | None = None

    def in_group(self, group: str) -> bool:
        return group in self.groups

    @property
    def is_member(self) -> bool:
        return GROUP_MEMBER in self.groups

    @property
    def is_reviewer(self) -> bool:
        return GROUP_REVIEWER in self.groups

    @property
    def is_admin(self) -> bool:
        return GROUP_ADMIN in self.groups
