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
