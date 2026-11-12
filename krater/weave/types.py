"""Data carried out of the Weave adapter.

`WeaveIdentity` comes from a freshly verified id_token, at sign-in time. `WeaveUser` comes from the
directory API, and is what Weave says about a user *right now*. Both carry Krater's role names
(`ganymede:*`), already translated from Weave's patch role keys, app role keys or group slugs by
`krater.weave.roles`.
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
    leave both fields out. Krater's verification check (`krater.services.clearance`) reads them.

    `demographics_answered` is true when the member has saved an answer to Weave's demographics form, and
    `None` when Weave didn't say (older Weave versions). Weave never sends the answers. Krater's
    demographics gate (`krater.services.demographics`) reads it.

    `us_resident` is Weave's US residency result: true or false while a result stands, `None` when Weave has no
    standing result or didn't say. It is residency, not citizenship, and Weave never sends the state. The OSPool
    rule (`krater.services.residency`) reads it.

    `verification_status` is Weave's identity verification: `needs_submission`, `pending`, `verified` or
    `ineligible`, and `None` when Weave didn't say. Only `verified` clears a member to use a project's compute
    (`krater.services.clearance`).

    `slack_required` is false when the member is in a sponsored project's group that waives the Patchwork Slack, and
    `code_of_conduct_required` false when such a group waives the code of conduct or an admin exempted them. Both are
    `None` when Weave didn't say (before weave#274). The Slack membership gate reads them."""

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
    demographics_answered: bool | None = None
    us_resident: bool | None = None
    verification_status: str | None = None
    slack_required: bool | None = None
    code_of_conduct_required: bool | None = None
