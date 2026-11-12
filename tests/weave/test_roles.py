"""`krater.weave.roles.RoleMapping`: everyone Weave lets use Krater is a member, the `roles` claim decides the
other roles, and group slugs only count when the `roles` field is absent."""

from __future__ import annotations

import pytest

from krater.config import Settings
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.roles import RoleMapping


@pytest.fixture
def mapping() -> RoleMapping:
    return RoleMapping.default()


def test_role_keys_map_to_krater_role_names(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["reviewer", "admin"], groups=[]) == frozenset(
        {GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN}
    )


def test_everyone_weave_returns_is_a_member_with_no_role(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=[], groups=[]) == frozenset({GROUP_MEMBER})


def test_unknown_role_keys_are_ignored(mapping: RoleMapping) -> None:
    # `member` is no Weave role any more: an app that still defines it gives nothing extra.
    assert mapping.krater_roles(roles=["member", "owner", 7], groups=[]) == frozenset({GROUP_MEMBER})


def test_the_roles_claim_wins_over_groups(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["reviewer"], groups=["krater-admins"]) == frozenset(
        {GROUP_MEMBER, GROUP_REVIEWER}
    )


def test_an_empty_roles_claim_does_not_fall_back_to_groups(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=[], groups=["krater-reviewers", "krater-admins"]) == frozenset({GROUP_MEMBER})


def test_an_absent_roles_claim_falls_back_to_group_slugs(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=None, groups=["krater-reviewers", "other-group"])

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


@pytest.mark.parametrize("bad", ["admin", {"admin": True}, 3])
def test_a_malformed_roles_value_gives_no_weave_role(mapping: RoleMapping, bad: object) -> None:
    assert mapping.krater_roles(roles=bad, groups=["krater-admins"]) == frozenset({GROUP_MEMBER})


def test_settings_change_the_keys_and_slugs() -> None:
    mapping = RoleMapping.from_settings(
        Settings(_env_file=None, weave_role_admin="boss", weave_group_reviewer="judges")
    )

    assert mapping.krater_roles(roles=["boss"], groups=[]) == frozenset({GROUP_MEMBER, GROUP_ADMIN})
    assert mapping.krater_roles(roles=None, groups=["judges"]) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    assert mapping.weave_role_key(GROUP_ADMIN) == "boss"


@pytest.mark.parametrize("krater_role", [GROUP_MEMBER, "ganymede:reviewer:senior"])
def test_weave_role_key_rejects_a_role_weave_doesnt_give(mapping: RoleMapping, krater_role: str) -> None:
    with pytest.raises(ValueError):
        mapping.weave_role_key(krater_role)


# -- Patch roles (the `patches` claim), and the union with app roles during the move --------------------------


def test_patch_role_keys_map_to_krater_role_names(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=[], groups=[], patches={"ganymede": ["krater:admin", "krater:reviewer"]})

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN})


def test_patch_roles_alone_count_without_a_roles_claim(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=None, groups=[], patches={"ganymede": ["krater:reviewer"]})

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


def test_app_roles_alone_still_count_with_an_empty_patches_claim(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["admin"], groups=[], patches={}) == frozenset({GROUP_MEMBER, GROUP_ADMIN})


def test_app_roles_and_patch_roles_are_a_union(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=["reviewer"], groups=[], patches={"ganymede": ["krater:admin"]})

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN})


def test_the_group_fallback_and_patch_roles_are_a_union(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=None, groups=["krater-admins"], patches={"ganymede": ["krater:reviewer"]})

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN})


def test_another_patchs_keys_give_nothing(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=[], groups=[], patches={"europa": ["krater:admin"]}) == frozenset({GROUP_MEMBER})


def test_unknown_keys_in_the_patch_are_ignored(mapping: RoleMapping) -> None:
    # `lead` is the governance patch lead, `quilt:viewer` is Quilt's key, and a bare `reviewer` is the app key,
    # which counts only in `roles`.
    patches = {"ganymede": ["lead", "quilt:viewer", "reviewer", "admin", "krater:member"]}

    assert mapping.krater_roles(roles=[], groups=[], patches=patches) == frozenset({GROUP_MEMBER})


@pytest.mark.parametrize(
    "bad",
    [
        ["ganymede", "krater:admin"],
        "krater:admin",
        7,
        {"ganymede": "krater:admin"},
        {"ganymede": {"krater:admin": True}},
        {"ganymede": None},
        {"ganymede": ["krater:admin", 7]},
        {"ganymede": ["krater:admin", None]},
    ],
)
def test_a_malformed_patches_value_gives_no_patch_role(mapping: RoleMapping, bad: object) -> None:
    assert mapping.krater_roles(roles=[], groups=[], patches=bad) == frozenset({GROUP_MEMBER})


def test_a_malformed_patches_value_keeps_the_app_roles(mapping: RoleMapping) -> None:
    # Fail closed for the patch source only: the `roles` claim still counts.
    roles = mapping.krater_roles(roles=["reviewer"], groups=[], patches={"ganymede": ["krater:admin", 7]})

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


def test_a_malformed_value_for_another_patch_does_not_matter(mapping: RoleMapping) -> None:
    patches = {"ganymede": ["krater:admin"], "europa": "oops"}

    assert mapping.krater_roles(roles=[], groups=[], patches=patches) == frozenset({GROUP_MEMBER, GROUP_ADMIN})


def test_settings_change_the_patch_and_its_keys() -> None:
    mapping = RoleMapping.from_settings(
        Settings(_env_file=None, weave_patch="europa", weave_patch_role_admin="krater:boss")
    )

    assert mapping.krater_roles(roles=[], groups=[], patches={"europa": ["krater:boss", "krater:reviewer"]}) == (
        frozenset({GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN})
    )
    assert mapping.krater_roles(roles=[], groups=[], patches={"ganymede": ["krater:admin"]}) == frozenset(
        {GROUP_MEMBER}
    )


def test_patch_role_key_names_the_patch_key(mapping: RoleMapping) -> None:
    assert mapping.patch_role_key(GROUP_REVIEWER) == "krater:reviewer"
    assert mapping.patch_role_key(GROUP_ADMIN) == "krater:admin"
    with pytest.raises(ValueError):
        mapping.patch_role_key(GROUP_MEMBER)
