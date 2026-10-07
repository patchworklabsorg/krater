"""`StubWeaveClient`: the bundled fixture, the exchange and directory behavior other tests rely on, and
the helpers tests use to change a user mid-test."""

from __future__ import annotations

import pytest

from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.errors import WeaveAuthError
from krater.weave.roles import RoleMapping
from krater.weave.stub import StubWeaveClient


@pytest.fixture
def stub_client() -> StubWeaveClient:
    return StubWeaveClient()


def test_bundled_fixture_covers_every_role(stub_client: StubWeaveClient) -> None:
    users = stub_client.list_all_users()
    assert len(users) >= 6

    members = [u for u in users if GROUP_MEMBER in u.roles]
    reviewers = [u for u in users if GROUP_REVIEWER in u.roles]
    admins = [u for u in users if GROUP_ADMIN in u.roles]
    non_members = [u for u in users if GROUP_MEMBER not in u.roles]

    assert len(members) >= 4  # plain members + reviewers + admin all hold member
    assert len(reviewers) >= 2
    assert len(admins) >= 1
    assert len(non_members) >= 1


def test_the_fixture_exercises_the_group_fallback(stub_client: StubWeaveClient) -> None:
    # PWLREVIEWERTWO has no `roles` field, so its group slugs decide.
    record = stub_client.get_user("PWLREVIEWERTWO")

    assert record is not None
    assert record.roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


def test_exchange_code_carries_the_roles_and_slack_claims(stub_client: StubWeaveClient) -> None:
    identity = stub_client.exchange_code(
        code="PWLMEMBERONE", code_verifier="unused", redirect_uri="http://testserver/auth/callback", nonce="unused"
    )

    assert identity.sub == "PWLMEMBERONE"
    assert identity.email_verified is True
    assert identity.slack_id == "U0001MEMBER"
    assert identity.roles == frozenset({GROUP_MEMBER})


def test_exchange_code_rejects_an_unknown_code(stub_client: StubWeaveClient) -> None:
    with pytest.raises(WeaveAuthError):
        stub_client.exchange_code(
            code="not-a-real-sub", code_verifier="v", redirect_uri="http://testserver/auth/callback", nonce="n"
        )


def test_directory_lookups(stub_client: StubWeaveClient) -> None:
    assert stub_client.get_user("does-not-exist") is None
    reviewers = {u.sub for u in stub_client.list_users_with_role(GROUP_REVIEWER)}
    assert reviewers == {"PWLREVIEWERONE", "PWLREVIEWERTWO", "PWLADMINONE"}


def test_set_roles_and_remove_user_change_later_lookups(stub_client: StubWeaveClient) -> None:
    stub_client.set_roles("PWLREVIEWERONE", ["member"])
    stub_client.set_active("PWLMEMBERTWO", False)
    stub_client.remove_user("PWLMEMBERONE")

    record = stub_client.get_user("PWLREVIEWERONE")
    assert record is not None and record.roles == frozenset({GROUP_MEMBER})
    inactive = stub_client.get_user("PWLMEMBERTWO")
    assert inactive is not None and inactive.active is False
    assert stub_client.get_user("PWLMEMBERONE") is None


def test_a_custom_role_mapping_is_applied(tmp_path) -> None:
    fixture = tmp_path / "users.json"
    fixture.write_text('[{"sub": "PWLX", "name": "X", "email": "x@example.com", "roles": ["krater-member"]}]')
    default = RoleMapping.default()
    mapping = RoleMapping(role_keys={**default.role_keys, GROUP_MEMBER: "krater-member"}, group_slugs={})

    record = StubWeaveClient(fixture, role_mapping=mapping).get_user("PWLX")

    assert record is not None and record.roles == frozenset({GROUP_MEMBER})


def test_a_minimal_fixture_entry_defaults_to_verified_and_active_with_no_roles(tmp_path) -> None:
    fixture = tmp_path / "users.json"
    fixture.write_text('[{"sub": "PWLMIN", "name": "Min", "email": "min@example.com"}]')

    record = StubWeaveClient(fixture).get_user("PWLMIN")

    assert record is not None
    assert record.email_verified is True
    assert record.active is True
    assert record.slack_id is None
    assert record.slack_member is None
    assert record.roles == frozenset()
