"""`StubWeaveClient`: a fake Weave for `KRATER_WEAVE_MODE=stub` (development and tests).

Makes no network calls. Users come from a JSON fixture (`weave_stub_users_file`, or the bundled
`stub_users.json` when that setting is blank). `authorization_url` points at the local `/auth/stub`
picker page; `exchange_code` treats the authorization code as the chosen user's `sub` directly.

Each fixture user has the same fields a real Weave sends: `sub`, `name`, `email`, `email_verified`,
`slack_id`, optional `slack_member`, `groups` (group slugs) and `roles` (Weave role keys; leave it out
to exercise the group-slug fallback). Sign-in and the directory lookups translate them with the same
`RoleMapping` the live client uses. Tests change a user mid-test with `set_roles`, `set_active` or
`remove_user`. The app refuses stub mode in production.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from krater.weave.errors import WeaveAuthError
from krater.weave.roles import RoleMapping
from krater.weave.types import WeaveIdentity, WeaveUser

#: The bundled fixture, used whenever `settings.weave_stub_users_file` is blank.
DEFAULT_STUB_USERS_FILE = Path(__file__).parent / "stub_users.json"


@dataclass(frozen=True)
class StubUser:
    """One fixture user, as Weave would describe them (role keys and group slugs, not Krater names)."""

    sub: str
    name: str
    email: str
    email_verified: bool
    slack_id: str | None
    slack_member: bool | None
    groups: tuple[str, ...]
    roles: tuple[str, ...] | None  # None: Weave sends no `roles` field
    active: bool


class StubWeaveClient:
    """A `WeaveClient` backed by a local JSON fixture of fake users. See the module docstring for the
    fixture's shape."""

    def __init__(self, stub_users_file: str | Path | None = None, *, role_mapping: RoleMapping | None = None) -> None:
        path = Path(stub_users_file) if stub_users_file else DEFAULT_STUB_USERS_FILE
        raw_users = json.loads(path.read_text())
        self._mapping = role_mapping if role_mapping is not None else RoleMapping.default()

        self._users_by_sub: dict[str, StubUser] = {}
        for entry in raw_users:
            raw_roles = entry.get("roles")
            user = StubUser(
                sub=entry["sub"],
                name=entry["name"],
                email=entry["email"],
                email_verified=entry.get("email_verified", True),
                slack_id=entry.get("slack_id"),
                slack_member=entry.get("slack_member"),
                groups=tuple(entry.get("groups", [])),
                roles=None if raw_roles is None else tuple(raw_roles),
                active=entry.get("active", True),
            )
            self._users_by_sub[user.sub] = user

    # -- WeaveClient protocol ------------------------------------------------------------------------

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        del code_verifier  # no real PKCE exchange happens in stub mode
        params = urlencode({"state": state, "nonce": nonce, "redirect_uri": redirect_uri})
        return f"/auth/stub?{params}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        del code_verifier, redirect_uri, nonce  # the stub trusts the code (a fixture `sub`) directly
        user = self._users_by_sub.get(code)
        if user is None:
            raise WeaveAuthError(f"no stub user with sub {code!r}")
        record = self._record(user)
        return WeaveIdentity(
            sub=record.sub,
            name=record.name,
            email=record.email,
            email_verified=record.email_verified,
            slack_id=record.slack_id,
            slack_member=record.slack_member,
            roles=record.roles,
        )

    def get_user(self, sub: str) -> WeaveUser | None:
        user = self._users_by_sub.get(sub)
        return None if user is None else self._record(user)

    def list_users_with_role(self, role: str) -> list[WeaveUser]:
        records = (self._record(user) for user in self._users_by_sub.values())
        return [record for record in records if role in record.roles]

    # -- Stub-only helpers ---------------------------------------------------------------------------

    def list_all_users(self) -> list[WeaveUser]:
        """Every fixture user, in file order, with Krater role names. Used by the `/auth/stub` picker."""
        return [self._record(user) for user in self._users_by_sub.values()]

    def put_user(
        self,
        sub: str,
        *,
        name: str = "",
        email: str = "",
        email_verified: bool = True,
        slack_id: str | None = None,
        slack_member: bool | None = None,
        roles: Iterable[str] | None = (),
        groups: Iterable[str] = (),
        active: bool = True,
    ) -> None:
        """Add or replace a user. `roles` are Weave role keys (`None` for no `roles` field at all) and
        `groups` are group slugs, as a real Weave would send them."""
        self._users_by_sub[sub] = StubUser(
            sub=sub,
            name=name,
            email=email,
            email_verified=email_verified,
            slack_id=slack_id,
            slack_member=slack_member,
            groups=tuple(groups),
            roles=None if roles is None else tuple(roles),
            active=active,
        )

    def set_roles(self, sub: str, roles: Iterable[str] | None, *, groups: Iterable[str] | None = None) -> None:
        """Change what Weave says `sub` holds: Weave role keys (`None` for no `roles` field at all) and,
        optionally, group slugs. For tests that revoke or grant a role mid-test."""
        user = self._users_by_sub[sub]
        changes: dict = {"roles": None if roles is None else tuple(roles)}
        if groups is not None:
            changes["groups"] = tuple(groups)
        self._users_by_sub[sub] = dataclasses.replace(user, **changes)

    def set_active(self, sub: str, active: bool) -> None:
        """Lock (`False`) or unlock a user, as a Weave admin would."""
        self._users_by_sub[sub] = dataclasses.replace(self._users_by_sub[sub], active=active)

    def remove_user(self, sub: str) -> None:
        """Make the directory answer 404 for `sub`, as Weave does for a user Krater may not see."""
        self._users_by_sub.pop(sub, None)

    def _record(self, user: StubUser) -> WeaveUser:
        return WeaveUser(
            sub=user.sub,
            name=user.name,
            email=user.email,
            email_verified=user.email_verified,
            slack_id=user.slack_id,
            slack_member=user.slack_member,
            roles=self._mapping.krater_roles(roles=user.roles, groups=user.groups),
            active=user.active,
        )


__all__ = ["DEFAULT_STUB_USERS_FILE", "StubUser", "StubWeaveClient"]
