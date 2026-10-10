"""`krater.weave.roles.RoleMapping`: the `roles` claim decides, and group slugs only count when the
`roles` field is absent."""

from __future__ import annotations

import pytest

from krater.config import Settings
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.roles import RoleMapping


@pytest.fixture
def mapping() -> RoleMapping:
    return RoleMapping.default()


def test_role_keys_map_to_krater_role_names(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["member", "reviewer", "admin"], groups=[]) == frozenset(
        {GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN}
    )


def test_unknown_role_keys_are_ignored(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["member", "owner", 7], groups=[]) == frozenset({GROUP_MEMBER})


def test_the_roles_claim_wins_over_groups(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["member"], groups=["krater-admins"]) == frozenset({GROUP_MEMBER})


def test_an_empty_roles_claim_does_not_fall_back_to_groups(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=[], groups=["ganymede-members", "krater-admins"]) == frozenset()


def test_an_absent_roles_claim_falls_back_to_group_slugs(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=None, groups=["ganymede-members", "krater-reviewers", "other-group"])

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


@pytest.mark.parametrize("bad", ["member", {"member": True}, 3])
def test_a_malformed_roles_value_fails_closed(mapping: RoleMapping, bad: object) -> None:
    assert mapping.krater_roles(roles=bad, groups=["ganymede-members"]) == frozenset()


def test_settings_change_the_keys_and_slugs() -> None:
    mapping = RoleMapping.from_settings(
        Settings(_env_file=None, weave_role_admin="boss", weave_group_member="everyone")
    )

    assert mapping.krater_roles(roles=["boss"], groups=[]) == frozenset({GROUP_ADMIN})
    assert mapping.krater_roles(roles=None, groups=["everyone"]) == frozenset({GROUP_MEMBER})
    assert mapping.weave_role_key(GROUP_ADMIN) == "boss"


def test_weave_role_key_rejects_an_unknown_krater_role(mapping: RoleMapping) -> None:
    with pytest.raises(ValueError):
        mapping.weave_role_key("ganymede:reviewer:senior")
