"""The guardian gate: the safeguard for team members under 18 (docs/SPEC.md "Guardian verification").

Weave asks a parent or guardian to consent by email for a member under 18. An email link proves control of an
inbox, not parenthood. So a Weave admin also records a stronger check in Weave: a signed guardian form plus a
short call or video check with the guardian. Weave's directory then says `guardian_verified: true`. Krater
stores none of this. It reads the directory and grants no budget to a project while a team member is a minor
without a verified guardian.

- **The team** is the project's submitter plus the credited builders on the revision being decided.
- **The rule.** A member blocks the grant when Weave says `minor: true` and `guardian_verified` is not
  `true` (false or missing). The directory is asked fresh at decision time, the same way role checks are.
- **No directory record.** Weave's directory answers 404 for anyone the Krater app may not serve. That includes
  a minor whose guardian consent is missing or was revoked, so a 404 blocks the grant too.
- **Unknown age.** Older Weave versions send a record without `minor`. Weave ships the fields first, so Krater
  treats that record as "not known to be a minor" and logs a warning, rather than blocking every grant while
  the rollout finishes.
- **Weave down.** If Weave can't be reached, the grant fails (`GuardianCheckUnavailable`). Krater never
  guesses.
- **No override.** This is a safety rule, so an admin override (`admin_decide`, `admin_adjust_budget`) can't
  bypass it. A Weave admin verifies the guardian first.

`require_team_cleared` is the gate. `krater.services.projects` calls it on every grant: a proposal approval, an
amendment approval that raises the ceiling, and an admin budget increase. The web and Slack review paths both
reach it through `record_review`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import Project, ProjectRevision, User
from krater.services.errors import GuardianCheckUnavailable, GuardianVerificationRequired
from krater.weave import WeaveClient, WeaveUnavailableError

logger = logging.getLogger(__name__)


#: Why a team member blocks a grant, as shown to reviewers and admins.
REASON_NO_VERIFIED_GUARDIAN = "under 18 without a verified guardian in Weave"
REASON_NOT_IN_WEAVE = "not available in Weave (no access to Krater)"


@dataclass(frozen=True)
class PendingMember:
    """A team member who blocks a budget grant: a minor whose guardian Weave hasn't verified, or a member
    Weave's directory doesn't return. `reason` is one of the `REASON_*` texts."""

    user: User
    name: str
    reason: str = REASON_NO_VERIFIED_GUARDIAN

    @property
    def label(self) -> str:
        """The member's name and Patchwork ID, for messages to reviewers and admins."""
        sub = self.user.weave_sub
        return f"{self.name} ({sub})" if self.name and self.name != sub else sub


def team_users(session: Session, project: Project, *, revision: ProjectRevision | None = None) -> list[User]:
    """The project's team: its submitter, then the credited builders on `revision` (the current revision by
    default). In a stable order, without duplicates."""
    revision = revision if revision is not None else project.current_revision
    users = [project.submitter]
    builder_ids = [
        uid for uid in (revision.credited_builder_ids if revision is not None else []) if uid != project.submitter_id
    ]
    if builder_ids:
        users.extend(session.scalars(sa.select(User).where(User.id.in_(builder_ids)).order_by(User.weave_sub)))
    return users


def members_needing_verification(
    session: Session,
    weave_client: WeaveClient,
    project: Project,
    *,
    revision: ProjectRevision | None = None,
    fresh: bool = True,
) -> list[PendingMember]:
    """Every team member who blocks a grant: Weave lists them as `minor: true` without `guardian_verified: true`,
    or Weave's directory has no record of them (404). A record without `minor` doesn't block; it is logged.

    `fresh` (the default) skips the directory client's cache, as every decision must; only a page view may
    pass `fresh=False`. Raises `WeaveUnavailableError` if Weave can't be reached.
    """
    pending: list[PendingMember] = []
    for user in team_users(session, project, revision=revision):
        record = weave_client.get_user(user.weave_sub, fresh=fresh)
        if record is None:
            # Weave hides anyone Krater may not serve, a minor without guardian consent included.
            pending.append(PendingMember(user=user, name="", reason=REASON_NOT_IN_WEAVE))
            continue
        if record.minor is None:
            logger.warning(
                "Weave didn't say whether team member %s of project %s is a minor (no minor field); treating them "
                "as not known to be a minor",
                user.weave_sub,
                project.id,
            )
            continue
        if record.minor and record.guardian_verified is not True:
            pending.append(PendingMember(user=user, name=record.name))
    return pending


def require_team_cleared(
    session: Session, weave_client: WeaveClient, project: Project, *, revision: ProjectRevision | None = None
) -> None:
    """The gate before any budget grant. Raises `GuardianVerificationRequired` (listing the members) if a team
    member is a minor without a verified guardian in Weave, and `GuardianCheckUnavailable` if Weave can't be
    reached. Both are `InvalidState`, so the web and Slack paths show the message and roll back."""
    try:
        pending = members_needing_verification(session, weave_client, project, revision=revision, fresh=True)
    except WeaveUnavailableError as exc:
        raise GuardianCheckUnavailable() from exc
    if pending:
        raise GuardianVerificationRequired([(member.label, member.reason) for member in pending])


def pending_for_display(session: Session, weave_client: WeaveClient, project: Project) -> list[PendingMember] | None:
    """`members_needing_verification` for a page view: may use the directory cache, and gives `None` instead
    of failing the page when Weave can't be reached."""
    try:
        return members_needing_verification(session, weave_client, project, fresh=False)
    except WeaveUnavailableError:
        logger.warning("Weave unavailable; can't show guardian status for project %s", project.id)
        return None


__all__ = [
    "REASON_NOT_IN_WEAVE",
    "REASON_NO_VERIFIED_GUARDIAN",
    "PendingMember",
    "members_needing_verification",
    "pending_for_display",
    "require_team_cleared",
    "team_users",
]
