"""`krater.services.users`: sign-in from the id_token's claims, and `authorize`, the fresh Weave
directory check every action goes through."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.services.errors import NotAMember
from krater.services.users import authorize, cached_actor, sign_in, upsert_user_from_identity
from krater.weave import StubWeaveClient, WeaveUnavailableError
from krater.weave.types import WeaveIdentity, WeaveUser


def _identity(
    sub: str = "PWLSIGNIN01",
    *,
    roles: frozenset[str] = frozenset({GROUP_MEMBER}),
    slack_id: str | None = None,
    name: str = "Sig Nin",
    email: str = "signin@example.com",
) -> WeaveIdentity:
    return WeaveIdentity(sub=sub, name=name, email=email, email_verified=True, slack_id=slack_id, roles=roles)


def test_upsert_creates_then_updates_the_same_user(db_session: Session) -> None:
    created = upsert_user_from_identity(db_session, _identity(name="Original", slack_id="U_ONE"))
    updated = upsert_user_from_identity(
        db_session, _identity(name="Renamed", email="new@example.com", roles=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    )

    assert updated.id == created.id
    assert updated.display_name == "Renamed"
    assert updated.email == "new@example.com"
    assert updated.roles_cached == [GROUP_ADMIN, GROUP_MEMBER]
    # Weave sent no slack_id the second time: the stored one stays.
    assert updated.slack_user_id == "U_ONE"


def test_a_slack_id_from_weave_moves_off_a_stale_holder(db_session: Session, make_user) -> None:
    stale = make_user(slack_user_id="U_SHARED")

    user = upsert_user_from_identity(db_session, _identity(slack_id="U_SHARED"))

    assert user.slack_user_id == "U_SHARED"
    db_session.refresh(stale)
    assert stale.slack_user_id is None


def test_a_member_signs_in_and_last_login_is_stamped(db_session: Session) -> None:
    result = sign_in(db_session, _identity())

    assert result.status == "ok"
    assert result.user.last_login_at is not None


def test_sign_in_rejects_a_user_without_the_member_role(db_session: Session) -> None:
    result = sign_in(db_session, _identity(roles=frozenset({GROUP_REVIEWER})))

    assert result.status == "not_a_member"
    assert result.user.last_login_at is None
    assert result.user.roles_cached == [GROUP_REVIEWER]


def test_cached_actor_reads_roles_cached(db_session: Session) -> None:
    user = sign_in(db_session, _identity(roles=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))).user

    assert cached_actor(user).groups == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


# -- authorize ---------------------------------------------------------------------------------------


@pytest.fixture
def signed_in(db_session: Session, weave: StubWeaveClient):
    weave.put_user("PWLSIGNIN01", name="Sig Nin", email="signin@example.com", roles=["member", "reviewer"])
    return sign_in(db_session, _identity(roles=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))).user


def test_authorize_builds_an_actor_from_the_directory(db_session: Session, weave: StubWeaveClient, signed_in) -> None:
    weave.put_user(
        "PWLSIGNIN01",
        name="New Name",
        email="signin@example.com",
        roles=["member"],
        slack_id="U_DIR",
        slack_member=True,
    )

    actor = authorize(db_session, weave, signed_in)

    assert actor.groups == frozenset({GROUP_MEMBER})
    assert actor.slack_member is True
    # The cached copies follow Weave.
    assert signed_in.roles_cached == [GROUP_MEMBER]
    assert signed_in.display_name == "New Name"
    assert signed_in.slack_user_id == "U_DIR"


def test_authorize_uses_the_group_fallback_when_roles_are_absent(
    db_session: Session, weave: StubWeaveClient, signed_in
) -> None:
    weave.set_roles("PWLSIGNIN01", None, groups=["ganymede-members", "krater-admins"])

    assert authorize(db_session, weave, signed_in).groups == frozenset({GROUP_MEMBER, GROUP_ADMIN})


def test_authorize_refuses_a_user_the_directory_no_longer_returns(
    db_session: Session, weave: StubWeaveClient, signed_in
) -> None:
    weave.remove_user("PWLSIGNIN01")

    with pytest.raises(NotAMember):
        authorize(db_session, weave, signed_in)


def test_authorize_refuses_once_weave_revokes_the_member_role(
    db_session: Session, weave: StubWeaveClient, signed_in
) -> None:
    weave.set_roles("PWLSIGNIN01", ["reviewer"])

    with pytest.raises(NotAMember):
        authorize(db_session, weave, signed_in)
    assert signed_in.roles_cached == [GROUP_REVIEWER]


def test_authorize_refuses_an_inactive_user(db_session: Session, weave: StubWeaveClient, signed_in) -> None:
    weave.set_active("PWLSIGNIN01", False)

    with pytest.raises(NotAMember):
        authorize(db_session, weave, signed_in)


def test_authorize_fails_closed_when_weave_is_down(db_session: Session, signed_in) -> None:
    class _DownWeave:
        def get_user(self, sub: str, *, fresh: bool = False) -> WeaveUser | None:
            raise WeaveUnavailableError("down")

    with pytest.raises(WeaveUnavailableError):
        authorize(db_session, _DownWeave(), signed_in)  # type: ignore[arg-type]


def test_authorize_asks_weave_uncached_unless_told_otherwise(
    db_session: Session, weave: StubWeaveClient, signed_in
) -> None:
    # The Slack click path relies on the default: a decision must never ride on a cached directory answer.
    seen: list[bool] = []
    real_get_user = weave.get_user

    def _recording(sub: str, *, fresh: bool = False) -> WeaveUser | None:
        seen.append(fresh)
        return real_get_user(sub, fresh=fresh)

    weave.get_user = _recording  # type: ignore[method-assign]

    authorize(db_session, weave, signed_in)
    authorize(db_session, weave, signed_in, fresh=False)

    assert seen == [True, False]
