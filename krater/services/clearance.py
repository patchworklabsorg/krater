"""Clearance: who may use a project's compute (docs/SPEC.md "Verification before funding").

A project can be submitted and approved, and its budget is granted on approval, before anyone is verified. Its
compute is held until every team member is cleared in Weave. Krater stores none of what Weave checked.

- **The team** is the project's lead plus its accepted members (`krater.services.guardian.team_users`), as it is
  when compute is asked for.
- **Cleared** means Weave's directory returns the member and says `verification_status: "verified"` (Weave's identity
  verification). For a member under 18 that pass already includes the guardian: Weave passes a minor only with a
  granted guardian consent and the parent or guardian on the call, and refuses Krater to a minor whose consent is
  missing or revoked (`krater.services.guardian`). Weave's separate `guardian_verified` field isn't needed.
- **Missing fields.** A record without `verification_status` doesn't clear the member: an older Weave holds compute
  rather than letting it through. A record without `minor` is "not known to be a minor", as before, and is logged.
- **No directory record.** Weave answers 404 for anyone the Krater app may not serve, so a 404 doesn't clear.
- **Weave down.** Nothing is cleared (`ClearanceCheckUnavailable`). Krater never guesses.
- **No override.** An admin can't clear a member in Krater: a Weave admin or the identity team does it in Weave.

Where it holds compute: a member queueing a job or resuming a stopped box (`krater.services.compute_jobs`), the
scheduler renting a machine for a queued job (`krater.compute.scheduler`), and someone joining a team whose project
already has budget (`krater.services.teams.accept`), since they could reach a running machine. A machine that is
already running isn't stopped when a member's verification lapses; their next rent waits.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from krater.models import Project, User
from krater.services.errors import ClearanceCheckUnavailable, VerificationRequired
from krater.services.guardian import MINOR_NOTE, REASON_NOT_IN_WEAVE, PendingMember, team_users
from krater.weave import WeaveClient, WeaveUnavailableError
from krater.weave.types import WeaveUser

logger = logging.getLogger(__name__)

#: Weave's `verification_status` that clears a member.
VERIFIED = "verified"
#: Why a member isn't cleared for ID verification, by Weave's `verification_status`, as shown on the project page.
REASONS_ID = {
    "needs_submission": "hasn't done ID verification in Weave",
    "pending": "ID verification in Weave is still in review",
    "ineligible": "didn't pass ID verification in Weave",
}
REASON_ID_UNKNOWN = "Weave didn't say whether they passed ID verification"


def reasons_for(record: WeaveUser | None, user: User, project: Project) -> list[str]:
    """Why `user` isn't cleared for `project`'s compute, from their directory `record`; empty when cleared."""
    if record is None:
        return [REASON_NOT_IN_WEAVE]
    if record.verification_status == VERIFIED:
        return []
    if record.verification_status is None:
        logger.warning("Weave sent no verification_status for %s; not clearing them", user.weave_sub)
    if record.minor is None:
        logger.warning(
            "Weave didn't say whether team member %s of project %s is a minor (no minor field); treating them "
            "as not known to be a minor",
            user.weave_sub,
            project.id,
        )
    reason = REASONS_ID.get(record.verification_status or "", REASON_ID_UNKNOWN)
    return [f"{reason}; {MINOR_NOTE}" if record.minor else reason]


def _check(weave_client: WeaveClient, user: User, project: Project, *, fresh: bool) -> PendingMember:
    record = weave_client.get_user(user.weave_sub, fresh=fresh)
    return PendingMember(
        user=user, name=record.name if record else "", reason="; ".join(reasons_for(record, user, project))
    )


def _pending(weave_client: WeaveClient, user: User, project: Project, *, fresh: bool) -> PendingMember | None:
    member = _check(weave_client, user, project, fresh=fresh)
    return None if member.cleared else member


def team_checks(
    session: Session, weave_client: WeaveClient, project: Project, *, fresh: bool = True
) -> list[PendingMember]:
    """Every team member, each with whether Weave clears them. `fresh` (the default) skips the directory cache, as
    every decision must; only a page view may pass `fresh=False`. Raises `WeaveUnavailableError`."""
    return [_check(weave_client, user, project, fresh=fresh) for user in team_users(session, project)]


def members_not_cleared(
    session: Session, weave_client: WeaveClient, project: Project, *, fresh: bool = True
) -> list[PendingMember]:
    """Every team member Weave hasn't cleared, with the reasons (`team_checks`). Raises `WeaveUnavailableError`."""
    return [member for member in team_checks(session, weave_client, project, fresh=fresh) if not member.cleared]


def require_team_cleared(session: Session, weave_client: WeaveClient, project: Project) -> None:
    """The gate before `project`'s compute is used. Raises `VerificationRequired` (listing the members) or
    `ClearanceCheckUnavailable`. Both are `InvalidState`, so the web, API and CLI show the message."""
    try:
        pending = members_not_cleared(session, weave_client, project, fresh=True)
    except WeaveUnavailableError as exc:
        raise ClearanceCheckUnavailable() from exc
    if pending:
        raise VerificationRequired([(member.label, member.reason) for member in pending])


def require_member_cleared(weave_client: WeaveClient, user: User, project: Project) -> None:
    """The same gate for one person joining a team whose project already has budget: they could reach a machine
    that is already running. Same errors as `require_team_cleared`."""
    try:
        member = _pending(weave_client, user, project, fresh=True)
    except WeaveUnavailableError as exc:
        raise ClearanceCheckUnavailable() from exc
    if member is not None:
        raise VerificationRequired([(member.label, member.reason)], joining=True)


def team_for_display(session: Session, weave_client: WeaveClient, project: Project) -> list[PendingMember] | None:
    """`team_checks` for a page view: may use the directory cache, and gives `None` instead of failing the page when
    Weave can't be reached."""
    try:
        return team_checks(session, weave_client, project, fresh=False)
    except WeaveUnavailableError:
        logger.warning("Weave unavailable; can't show the team's verification for project %s", project.id)
        return None


def pending_for_display(session: Session, weave_client: WeaveClient, project: Project) -> list[PendingMember] | None:
    """The members `team_for_display` doesn't clear, or `None` when Weave can't be reached."""
    team = team_for_display(session, weave_client, project)
    return None if team is None else [member for member in team if not member.cleared]


__all__ = [
    "REASONS_ID",
    "REASON_ID_UNKNOWN",
    "VERIFIED",
    "members_not_cleared",
    "pending_for_display",
    "reasons_for",
    "require_member_cleared",
    "require_team_cleared",
    "team_checks",
    "team_for_display",
]
