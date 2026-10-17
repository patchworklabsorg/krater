"""SSH public keys on a member's account: parsing (nothing but `type base64` is ever stored), the service rules, and
`/account/ssh-keys`."""

from __future__ import annotations

import base64

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient

from krater.models import UserSshKey
from krater.services import ssh_keys
from krater.services.errors import NotFound, ValidationFailed
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, get_csrf_token
from tests.web.conftest import actor_for


def _ed25519() -> str:
    key = ed25519.Ed25519PrivateKey.generate().public_key()
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def _rsa() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


# -- Parsing -------------------------------------------------------------------------------------------------


def test_a_real_key_parses_with_its_fingerprint() -> None:
    text = _ed25519() + " alice@laptop"

    parsed = ssh_keys.parse_public_key(text)

    assert parsed.key_type == "ssh-ed25519"
    assert parsed.comment == "alice@laptop"
    assert parsed.fingerprint.startswith("SHA256:") and "=" not in parsed.fingerprint
    assert ssh_keys.parse_public_key(_rsa()).key_type == "ssh-rsa"


@pytest.mark.parametrize(
    "make",
    [
        lambda k: 'command="rm -rf /" ' + k,  # options are never accepted
        lambda k: k + "\nssh-ed25519 AAAA second",  # nor a second line
        lambda k: "ssh-dss " + k.split()[1],  # nor other types
        lambda k: "ssh-rsa " + k.split()[1],  # nor a type that doesn't match the blob
        lambda k: "ssh-ed25519 not-base64!",
        lambda k: "ssh-ed25519 " + base64.b64encode(b"x").decode(),
        lambda k: "",
        lambda k: "-----BEGIN OPENSSH PRIVATE KEY-----",
    ],
)
def test_anything_else_is_refused(make) -> None:
    with pytest.raises(ValidationFailed):
        ssh_keys.parse_public_key(make(_ed25519()))


# -- Service ---------------------------------------------------------------------------------------------------


def test_add_list_remove_and_authorized_keys(login_as, db_session) -> None:
    member = login_as(MEMBER_SUB)
    actor = actor_for(member)
    text = _ed25519() + " laptop"

    key = ssh_keys.add_key(db_session, actor, name="", text=text)

    assert key.name == "laptop"
    assert ssh_keys.authorized_keys_for(db_session, member.id) == [" ".join(text.split()[:2])]
    with pytest.raises(ValidationFailed):
        ssh_keys.add_key(db_session, actor, name="again", text=text)
    other = actor_for(login_as(OTHER_MEMBER_SUB))
    with pytest.raises(NotFound):
        ssh_keys.remove_key(db_session, other, key_id=key.id)
    ssh_keys.remove_key(db_session, actor, key_id=key.id)
    assert ssh_keys.list_keys(db_session, member) == []


def test_an_email_comment_never_becomes_the_keys_name(login_as, db_session) -> None:
    actor = actor_for(login_as(MEMBER_SUB))

    key = ssh_keys.add_key(db_session, actor, name="", text=_ed25519() + " ada@laptop.example.com")

    assert key.name == "ssh-ed25519"


def test_keys_per_member_are_limited(login_as, db_session) -> None:
    actor = actor_for(login_as(MEMBER_SUB))
    for n in range(ssh_keys.MAX_KEYS_PER_USER):
        ssh_keys.add_key(db_session, actor, name=f"k{n}", text=_ed25519())

    with pytest.raises(ValidationFailed):
        ssh_keys.add_key(db_session, actor, name="one more", text=_ed25519())


# -- Routes ----------------------------------------------------------------------------------------------------


def test_the_page_requires_sign_in(client: TestClient) -> None:
    response = client.get("/account/ssh-keys", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_a_member_adds_and_removes_a_key(client: TestClient, login_as, db_session) -> None:
    member = login_as(MEMBER_SUB)
    page = client.get("/account/ssh-keys")
    assert page.status_code == 200 and "No keys yet." in page.text

    added = client.post(
        "/account/ssh-keys",
        data={"name": "Laptop", "key": _ed25519(), "csrf_token": get_csrf_token(page.text)},
        follow_redirects=False,
    )
    assert added.status_code == 303
    key = db_session.scalars(sa.select(UserSshKey).where(UserSshKey.user_id == member.id)).one()
    page = client.get("/account/ssh-keys")
    assert key.fingerprint in page.text

    removed = client.post(
        f"/account/ssh-keys/{key.id}/delete", data={"csrf_token": get_csrf_token(page.text)}, follow_redirects=False
    )
    assert removed.status_code == 303
    assert db_session.scalars(sa.select(UserSshKey).where(UserSshKey.user_id == member.id)).all() == []


def test_a_bad_key_redisplays_the_form(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)
    page = client.get("/account/ssh-keys")

    response = client.post(
        "/account/ssh-keys", data={"name": "x", "key": "not a key", "csrf_token": get_csrf_token(page.text)}
    )

    assert response.status_code == 422
    assert "Paste a public key" in response.text


def test_adding_needs_the_csrf_token(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)

    response = client.post("/account/ssh-keys", data={"name": "x", "key": _ed25519()})

    assert response.status_code == 403


def test_someone_who_lost_membership_cannot_add_keys(client: TestClient, login_as, weave_stub) -> None:
    login_as(MEMBER_SUB)
    page = client.get("/account/ssh-keys")
    weave_stub.set_roles(MEMBER_SUB, [])

    response = client.post(
        "/account/ssh-keys", data={"name": "x", "key": _ed25519(), "csrf_token": get_csrf_token(page.text)}
    )

    assert response.status_code == 403


def test_nobody_removes_someone_elses_key(client: TestClient, login_as, db_session) -> None:
    owner = login_as(MEMBER_SUB)
    key = ssh_keys.add_key(db_session, actor_for(owner), name="k", text=_ed25519())
    login_as(OTHER_MEMBER_SUB)
    page = client.get("/account/ssh-keys")

    response = client.post(f"/account/ssh-keys/{key.id}/delete", data={"csrf_token": get_csrf_token(page.text)})

    assert response.status_code == 404
    assert db_session.get(UserSshKey, key.id) is not None
