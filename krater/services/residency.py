"""The OSPool residency rule: only verified US residents may use OSPool capacity (docs/SPEC.md "OSPool").

OSPool (the Open Science Pool) runs NSF-funded donated capacity. It approves every user and every project after
Patchwork's own review, and only US residents may use it. Weave checks residency, not citizenship, at identity
verification, and its directory says `us_resident: true` while a yes result stands. Weave never sends the state.
Krater stores none of this.

- **The team** is the project's lead plus its accepted members (`krater.services.guardian.team_users`). OSPool
  approves every user, so one member who is not a verified US resident keeps the whole project off OSPool.
- **The rule.** A member counts only when Weave says `us_resident: true`. False, a missing field (an older Weave,
  or no standing result) and a 404 all count as "not a verified US resident". This fails closed, unlike the
  missing-`minor` rule: the OSPool rule is a partner's condition, and nothing else depends on it.
- **Weave down.** The gate fails (`UsResidencyCheckUnavailable`). Krater never guesses.

`require_us_residents` is the gate for the OSPool job type (docs/compute/ospool.md); call it before Krater submits
a job to OSPool. `resident_teams` gives reviewers the filter on `/reviews`, and `team_status_for_display` the line
on a project page, so they can pick the projects to send to OSPool. The gate's message names no member, because a
member may read it; the names are on the exception (`members`) for reviewers, admins and the log.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from krater.models import Project, User
from krater.services.errors import UsResidencyCheckUnavailable, UsResidencyRequired
from krater.services.guardian import team_users
from krater.weave import WeaveClient, WeaveUnavailableError

#: Why a team member is not counted, as shown to reviewers and admins.
REASON_NOT_RESIDENT = "not a verified US resident in Weave"
REASON_NOT_IN_WEAVE = "not available in Weave (no access to Krater)"


@dataclass(frozen=True)
class NonResidentMember:
    """A team member who keeps the project off OSPool. `reason` is one of the `REASON_*` texts."""

    user: User
    name: str
    reason: str = REASON_NOT_RESIDENT

    @property
    def label(self) -> str:
        """The member's name and Patchwork ID, for messages to reviewers and admins."""
        sub = self.user.weave_sub
        return f"{self.name} ({sub})" if self.name and self.name != sub else sub


@dataclass(frozen=True)
class TeamResidency:
    """The residency of a project's team for a page view: `all_residents` is true when every member is a verified
    US resident. `others` lists the members who are not."""

    team_size: int
    others: list[NonResidentMember]

    @property
    def all_residents(self) -> bool:
        return self.team_size > 0 and not self.others


def non_residents(
    session: Session, weave_client: WeaveClient, project: Project, *, fresh: bool = True
) -> list[NonResidentMember]:
    """Every team member who is not a verified US resident in Weave, or whom Weave's directory doesn't return.

    `fresh` (the default) skips the directory client's cache, as every decision must; only a page view may pass
    `fresh=False`. Raises `WeaveUnavailableError` if Weave can't be reached.
    """
    return [
        member
        for user in team_users(session, project)
        if (member := _not_resident(weave_client, user, fresh=fresh)) is not None
    ]


def _not_resident(weave_client: WeaveClient, user: User, *, fresh: bool) -> NonResidentMember | None:
    record = weave_client.get_user(user.weave_sub, fresh=fresh)
    if record is None:
        return NonResidentMember(user=user, name="", reason=REASON_NOT_IN_WEAVE)
    if record.us_resident is not True:
        return NonResidentMember(user=user, name=record.name)
    return None


def require_us_residents(session: Session, weave_client: WeaveClient, project: Project) -> None:
    """The gate before Krater uses OSPool capacity for `project`. Raises `UsResidencyRequired` (listing the
    members) if a team member is not a verified US resident, and `UsResidencyCheckUnavailable` if Weave can't be
    reached. Both are `InvalidState`."""
    try:
        others = non_residents(session, weave_client, project, fresh=True)
    except WeaveUnavailableError as exc:
        raise UsResidencyCheckUnavailable() from exc
    if others:
        raise UsResidencyRequired([(member.label, member.reason) for member in others])


def resident_teams(session: Session, weave_client: WeaveClient, projects: list[Project]) -> tuple[list[Project], bool]:
    """The projects in `projects` whose whole team is verified US residents, for a list page. Each member is looked
    up once (from the directory cache). Returns `(projects, unavailable)`: when Weave can't be reached, it stops at the
    first failure and gives `([], True)`, so a down Weave costs one timeout, not one for each project."""
    answers: dict[str, bool] = {}
    kept = []
    try:
        for project in projects:
            team = team_users(session, project)
            for user in team:
                if user.weave_sub not in answers:
                    answers[user.weave_sub] = _not_resident(weave_client, user, fresh=False) is None
            if team and all(answers[user.weave_sub] for user in team):
                kept.append(project)
    except WeaveUnavailableError:
        return [], True
    return kept, False


def team_status_for_display(session: Session, weave_client: WeaveClient, project: Project) -> TeamResidency | None:
    """The team's residency for a page view: may use the directory cache, and gives `None` instead of failing the
    page when Weave can't be reached."""
    try:
        team = team_users(session, project)
        others = [m for user in team if (m := _not_resident(weave_client, user, fresh=False)) is not None]
    except WeaveUnavailableError:
        return None
    return TeamResidency(team_size=len(team), others=others)


__all__ = [
    "REASON_NOT_IN_WEAVE",
    "REASON_NOT_RESIDENT",
    "NonResidentMember",
    "TeamResidency",
    "non_residents",
    "require_us_residents",
    "resident_teams",
    "team_status_for_display",
]
