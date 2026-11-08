"""`StubWeaveClient`: the bundled fixture, the exchange and directory behavior other tests rely on, and
the helpers tests use to change a user mid-test."""

from __future__ import annotations

import pytest

from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.errors import WeaveAuthError
from krater.weave.roles import RoleMapping
from krater.weave.stub import STUB_QUILT_TOKEN, STUB_STASH_TOKEN, StubWeaveClient


@pytest.fixture
def stub_client() -> StubWeaveClient:
    return StubWeaveClient()


def test_bundled_fixture_covers_every_role(stub_client: StubWeaveClient) -> None:
    users = stub_client.list_all_users()
    assert len(users) >= 6

    reviewers = [u for u in users if GROUP_REVIEWER in u.roles]
    admins = [u for u in users if GROUP_ADMIN in u.roles]

    # Everyone Weave lets use Krater is a member; there's no member role to leave out.
    assert all(GROUP_MEMBER in u.roles for u in users)
    assert len(reviewers) >= 2
    assert len(admins) >= 1


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


def test_a_minimal_fixture_entry_defaults_to_a_verified_active_member(tmp_path) -> None:
    fixture = tmp_path / "users.json"
    fixture.write_text('[{"sub": "PWLMIN", "name": "Min", "email": "min@example.com"}]')

    record = StubWeaveClient(fixture).get_user("PWLMIN")

    assert record is not None
    assert record.email_verified is True
    assert record.active is True
    assert record.slack_id is None
    assert record.slack_member is None
    assert record.roles == frozenset({GROUP_MEMBER})


def test_the_stub_quilt_token_is_a_fixed_fake_and_counts_calls() -> None:
    stub = StubWeaveClient()

    assert stub.quilt_token() == STUB_QUILT_TOKEN
    stub.invalidate_quilt_token()
    assert (stub.quilt_token_requests, stub.quilt_token_invalidations) == (1, 1)


def test_the_stub_stash_token_is_a_fixed_fake_and_counts_calls() -> None:
    stub = StubWeaveClient()

    assert stub.stash_token() == STUB_STASH_TOKEN
    assert stub.stash_token() != STUB_QUILT_TOKEN
    stub.invalidate_stash_token()
    assert (stub.stash_token_requests, stub.stash_token_invalidations) == (2, 1)
    assert (stub.quilt_token_requests, stub.quilt_token_invalidations) == (0, 0)


def test_the_fixture_has_a_minor_with_and_without_a_verified_guardian(stub_client: StubWeaveClient) -> None:
    unverified = stub_client.get_user("PWLMINORONE")
    verified = stub_client.get_user("PWLMINORVERIFIED")
    unknown = stub_client.get_user("PWLMEMBERTWO")

    assert unverified is not None and unverified.minor is True and unverified.guardian_verified is False
    assert verified is not None and verified.minor is True and verified.guardian_verified is True
    assert unknown is not None and unknown.minor is None and unknown.guardian_verified is None


def test_set_minor_and_set_guardian_verified_change_the_record(stub_client: StubWeaveClient) -> None:
    stub_client.set_minor("PWLMEMBERONE", True)
    stub_client.set_guardian_verified("PWLMEMBERONE", True)

    record = stub_client.get_user("PWLMEMBERONE")

    assert record is not None
    assert record.minor is True
    assert record.guardian_verified is True


def test_set_demographics_answered_changes_the_record(stub_client: StubWeaveClient) -> None:
    assert stub_client.get_user("PWLMEMBERONE").demographics_answered is None

    stub_client.set_demographics_answered("PWLMEMBERONE", False)

    record = stub_client.get_user("PWLMEMBERONE")
    assert record is not None
    assert record.demographics_answered is False


def test_set_us_resident_changes_the_record(stub_client: StubWeaveClient) -> None:
    assert stub_client.get_user("PWLMEMBERONE").us_resident is None

    stub_client.set_us_resident("PWLMEMBERONE", True)

    record = stub_client.get_user("PWLMEMBERONE")
    assert record is not None
    assert record.us_resident is True
