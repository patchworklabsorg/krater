"""Data carried out of the Weave adapter.

`WeaveIdentity` comes from a freshly verified id_token, at sign-in time. `WeaveUser` comes from the
directory API, and is what Weave says about a user *right now*. Both carry Krater's role names
(`ganymede:*`), already translated from Weave's role keys or group slugs by `krater.weave.roles`.
See `docs/weave-integration.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeaveIdentity:
    """A verified identity, built from an id_token's claims.

    `roles` holds Krater role names. `slack_member` is `None` when Weave didn't say.
    """

    sub: str
    name: str
    email: str
    email_verified: bool
    slack_id: str | None = None
    slack_member: bool | None = None
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WeaveUser:
    """A directory record. `roles` holds Krater role names. `slack_member` is `None` when Weave
    didn't say. `active` is false for an account Weave has locked or deactivated.

    `minor` is true when Weave knows the member is under 18, false when Weave knows they are not, and
    `None` when Weave didn't say. `guardian_verified` is true when a Weave admin recorded a signed
    guardian form plus a call or video check, and `None` when Weave didn't say. Older Weave versions
    leave both fields out. Krater's guardian gate (`krater.services.guardian`) reads them."""

    sub: str
    name: str
    email: str
    email_verified: bool
    slack_id: str | None
    slack_member: bool | None
    roles: frozenset[str]
    active: bool
    minor: bool | None = None
    guardian_verified: bool | None = None
