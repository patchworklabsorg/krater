"""Exception types raised by `krater.skypilot`.

Callers branch on these two, mirroring `krater.weave.errors`: one for "couldn't even reach or complete
the round trip with SkyPilot" (network error, timeout, a request stuck non-terminal past our deadline),
one for "SkyPilot understood the request but it failed" (a `FAILED` polled request, a non-2xx response
with a body we can show).
"""

from __future__ import annotations


class SkyPilotError(Exception):
    """Base class for every error raised by `krater.skypilot`."""


class SkyPilotUnavailableError(SkyPilotError):
    """SkyPilot couldn't be reached at all: a network error, a timeout, or a request whose polled
    status never reached SUCCEEDED/FAILED before our deadline."""


class SkyPilotRequestFailedError(SkyPilotError):
    """SkyPilot was reached and understood the request, but it failed. Carries the server's own
    message (from the polled request's `error`, or the HTTP response body) so callers can show it,
    plus, for a failed polled request, the server-side exception's class name (`error_type`, e.g.
    `"ClusterNotUpError"`), which the message alone doesn't include."""

    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


class SkyPilotWorkspaceNotFoundError(SkyPilotRequestFailedError):
    """The request was scoped to a workspace that doesn't exist on the SkyPilot server (deleted out of
    band, or SkyPilot's state was reset)."""
