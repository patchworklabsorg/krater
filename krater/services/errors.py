"""Domain errors raised by `krater.services`.

Services never raise anything else for expected failure modes (bad input, wrong state, missing
authorization, missing record) -- routers catch these and turn them into HTTP responses. Anything
else escaping a service is a bug.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error raised by a Krater service."""


class NotAllowed(DomainError):
    """The actor isn't authorized to perform this action."""


class NotAMember(NotAllowed):
    """Weave doesn't (or no longer does) list the user as an active Ganymede member."""


class InvalidState(DomainError):
    """The action doesn't make sense given the current status/outcome of the project or revision."""


class ValidationFailed(DomainError):
    """The input itself is invalid, independent of authorization or state.

    Carries a mapping of field name -> message so callers (routers, forms) can attach errors to the
    right field instead of showing one flat string.
    """

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{field}: {message}" for field, message in errors.items()))


class NotFound(DomainError):
    """The requested record doesn't exist (or isn't visible to this actor)."""


class ConfirmationRequired(DomainError):
    """The action is allowed, but costly enough that the member must confirm it first (the message says why). The
    caller shows the message and asks again with the confirmation set."""


class GuardianVerificationRequired(InvalidState):
    """Krater can't grant budget: a team member is under 18 without a verified guardian in Weave, or Weave's
    directory doesn't return them.

    `members` holds `(label, reason)` for each such member, so reviewers and admins see who blocks the grant and
    why (docs/SPEC.md "Guardian verification")."""

    def __init__(self, members: list[tuple[str, str]]) -> None:
        self.members = members
        listed = "; ".join(f"{label}: {reason}" for label, reason in members)
        super().__init__(
            "Krater can't grant budget to this project yet. These team members must be cleared in Weave first: "
            + listed
            + ". A Weave admin records a verified guardian (the signed form and the call) on the member's Weave "
            "admin page."
        )


class GuardianCheckUnavailable(InvalidState):
    """Krater couldn't ask Weave about the team members' ages, so it grants no budget (fail closed)."""

    def __init__(self) -> None:
        super().__init__(
            "Krater couldn't reach Weave to check the team members' ages, so it granted no budget. "
            "Try again in a minute."
        )


class DemographicsRequired(InvalidState):
    """The member who submits a grant application hasn't answered Weave's demographics form yet, or Weave's directory
    doesn't return them (docs/weave-integration.md "Demographics gate")."""

    def __init__(self) -> None:
        super().__init__(
            "Answer the demographics form in Weave before you submit. Funders need these answers. "
            "Krater never sees them. Your draft is saved."
        )


class DemographicsCheckUnavailable(InvalidState):
    """Krater couldn't ask Weave whether the member answered the demographics form, so it doesn't submit (fail
    closed)."""

    def __init__(self) -> None:
        super().__init__(
            "Krater couldn't reach Weave to check your demographics answers, so it didn't submit. "
            "Your draft is saved. Try again in a minute."
        )


class UsResidencyRequired(InvalidState):
    """The project can't use OSPool capacity: a team member is not a verified US resident in Weave, or Weave's
    directory doesn't return them (`krater.services.residency`).

    `members` holds `(label, reason)` for each such member, for reviewers, admins and the log. The message names
    nobody: a team member may read it, and only reviewers and admins see who is not a resident."""

    def __init__(self, members: list[tuple[str, str]]) -> None:
        self.members = members
        super().__init__(
            "This project can't use OSPool capacity. OSPool is open only to US residents, and not every team member "
            "is a verified US resident in Weave. Each member does the residency check on Weave's verification page."
        )


class UsResidencyCheckUnavailable(InvalidState):
    """Krater couldn't ask Weave about the team members' US residency, so it doesn't use OSPool capacity (fail
    closed)."""

    def __init__(self) -> None:
        super().__init__(
            "Krater couldn't reach Weave to check the team members' US residency, so it didn't use OSPool capacity. "
            "Try again in a minute."
        )


class FunderPacketUnavailable(InvalidState):
    """Krater couldn't ask Weave for the teams' names and US residency, so it made no funder packet
    (`krater.services.funder_packet`): what leaves Krater is checked just now, never from a cache."""

    def __init__(self) -> None:
        super().__init__(
            "Krater couldn't reach Weave for the teams' names and US residency, so it made no PDF. "
            "Try again in a minute."
        )
