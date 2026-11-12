"""How Weave's roles and groups become Krater's roles. The only place that knows this mapping.

Every Patchwork member Weave lets use Krater is a Ganymede member: Weave decides that with the Krater
app's access policy, so a user who signs in, or whom the directory returns, holds `ganymede:member`.
There is no member role in Weave.

Weave owns the other roles, and sends them two ways. Krater holds the union of both:

- Patch roles. Ganymede is a patch in Weave with its own role keys. Weave sends the keys a user holds
  per patch in the `patches` claim and in each directory record, e.g.
  `{"ganymede": ["krater:admin", "krater:reviewer", "lead"]}`. Every app of the patch gets the same
  claim, so Krater's keys carry the `krater:` prefix (by default `krater:reviewer` and `krater:admin`).
  Krater reads only its own patch (by default `ganymede`) and ignores every other key there (`lead`,
  `quilt:viewer`).
- App roles (legacy). Krater is an OAuth app in Weave with two app-defined role keys (by default
  `reviewer` and `admin`), sent in the `roles` claim and in each directory record. If Weave sends no
  `roles` field at all (absent, not empty), Krater falls back to the group slugs linked to the Krater
  app (by default `krater-reviewers`, `krater-admins`).

The union is for the move from app roles to patch roles: nobody loses access while Weave copies the app
roles into patch roles. A later change removes the app roles and groups from this mapping
(`docs/weave-integration.md`, "Patch roles").

Inside Krater, roles keep their `ganymede:*` names (`krater.services.actor`), so the `Actor`, approval
policies and review snapshots don't change. The translation happens here, at the `krater.weave`
boundary, and nothing outside this package sees Weave's keys, slugs or patch slug.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from krater.config import Settings
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER


@dataclass(frozen=True)
class RoleMapping:
    """Krater role name -> Weave app role key, Krater role name -> Weave group slug, and Krater role name ->
    Weave patch role key in the patch `patch_slug`, for the roles Weave gives (`ganymede:reviewer`,
    `ganymede:admin`)."""

    role_keys: Mapping[str, str]
    group_slugs: Mapping[str, str]
    patch_slug: str = "ganymede"
    patch_role_keys: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings) -> RoleMapping:
        return cls(
            role_keys={
                GROUP_REVIEWER: settings.weave_role_reviewer,
                GROUP_ADMIN: settings.weave_role_admin,
            },
            group_slugs={
                GROUP_REVIEWER: settings.weave_group_reviewer,
                GROUP_ADMIN: settings.weave_group_admin,
            },
            patch_slug=settings.weave_patch,
            patch_role_keys={
                GROUP_REVIEWER: settings.weave_patch_role_reviewer,
                GROUP_ADMIN: settings.weave_patch_role_admin,
            },
        )

    @classmethod
    def default(cls) -> RoleMapping:
        """The mapping with every setting at its default."""
        return cls.from_settings(Settings(_env_file=None))

    def krater_roles(self, *, roles: object, groups: object, patches: object = None) -> frozenset[str]:
        """Krater's role names for a user Weave let use Krater, from Weave's `roles`, `groups` and `patches` values.

        Always holds `ganymede:member`. The result is the union of the patch roles and the app roles.

        `roles` is `None` when Weave didn't send the field at all; only then are `groups` used. A `roles` value
        that isn't a list counts as holding no app role, so a malformed answer fails closed. Unknown keys and
        slugs, and non-string entries, are ignored.

        `patches` is `None` when Weave didn't send the field (the token or app lacks the `patches` scope). It
        counts as no patch role, as does any malformed value: not an object, a value for Krater's patch that
        isn't a list, or a list with a non-string entry. That fails closed for patch roles only. Other patches
        and unknown keys in Krater's patch are ignored.
        """
        app = self._match(self.role_keys, roles) if roles is not None else self._match(self.group_slugs, groups)
        return app | self._match(self.patch_role_keys, self._patch_keys(patches)) | {GROUP_MEMBER}

    def weave_role_key(self, krater_role: str) -> str:
        """The Weave app role key for a Krater role name, e.g. `ganymede:reviewer` -> `reviewer`. `ganymede:member`
        has none: every user Weave lets use Krater holds it."""
        try:
            return self.role_keys[krater_role]
        except KeyError:
            raise ValueError(f"no Weave role key for Krater role {krater_role!r}") from None

    def patch_role_key(self, krater_role: str) -> str:
        """The Weave patch role key for a Krater role name, e.g. `ganymede:reviewer` -> `krater:reviewer`."""
        try:
            return self.patch_role_keys[krater_role]
        except KeyError:
            raise ValueError(f"no Weave patch role key for Krater role {krater_role!r}") from None

    def _patch_keys(self, patches: object) -> frozenset[str]:
        """The keys the user holds in Krater's patch, or none if `patches` is missing or malformed."""
        if not isinstance(patches, Mapping):
            return frozenset()
        keys = patches.get(self.patch_slug)
        if not isinstance(keys, list | tuple) or not all(isinstance(key, str) for key in keys):
            return frozenset()
        return frozenset(keys)

    @staticmethod
    def _match(table: Mapping[str, str], values: object) -> frozenset[str]:
        held = _string_set(values)
        return frozenset(krater_role for krater_role, weave_name in table.items() if weave_name in held)


def _string_set(values: object) -> frozenset[str]:
    if not isinstance(values, Iterable) or isinstance(values, str | bytes | Mapping):
        return frozenset()
    return frozenset(value for value in values if isinstance(value, str))


__all__ = ["RoleMapping"]
