"""Admins' SSH access to every box (`krater.services.box_access`): the admin list synced from Weave, and the key list
the runner gets on start and in every heartbeat."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import AuditEvent, BoxAdmin
from krater.services import box_access, ssh_keys
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER
from krater.weave import WeaveUnavailableError
from tests.compute.test_boxes import _beat, _running_box
from tests.compute.test_scheduler import NOW, _submit
from tests.services.conftest import add_to_team


def _key(db_session, actor) -> str:
    public = ed25519.Ed25519PrivateKey.generate().public_key()
    text = public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    ssh_keys.add_key(db_session, actor, name="laptop", text=text)
    return text


@pytest.fixture
def box(db_session, member, project, client, fake):  # noqa: F811
    """A running box whose runner takes key updates, owned by `member` (who has one key)."""
    owner_key = _key(db_session, member)
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3, key_updates=True)
    return job, row, owner_key


AT = NOW + timedelta(minutes=4)


def _changes(db_session) -> list[dict]:
    return [event.payload for event in db_session.query(AuditEvent).filter_by(action=box_access.AUDIT_ADMINS_CHANGED)]


def test_every_admins_keys_go_on_every_box(db_session, admin, weave, box) -> None:
    job, row, owner_key = box
    admin_key = _key(db_session, admin)
    assert _beat(db_session, row, 4).ssh_keys == (owner_key,)  # not synced yet

    added, removed = box_access.sync_admins(db_session, weave, now=AT)

    assert (added, removed) == ([admin.user.weave_sub], [])
    assert _beat(db_session, row, 5).ssh_keys == (owner_key, admin_key)
    assert runner_api.start(db_session, row, now=AT, settings=get_settings()).ssh_keys == (owner_key, admin_key)
    assert _changes(db_session) == [{"added": [admin.user.weave_sub], "removed": []}]


def test_an_admin_removed_in_weave_leaves_the_boxes(db_session, admin, weave, box) -> None:
    _, row, owner_key = box
    _key(db_session, admin)
    box_access.sync_admins(db_session, weave, now=AT)

    weave.set_roles(admin.user.weave_sub, [])
    box_access.sync_admins(db_session, weave, now=AT + timedelta(minutes=5))

    assert _beat(db_session, row, 10).ssh_keys == (owner_key,)
    assert db_session.query(BoxAdmin).count() == 0
    assert {"added": [], "removed": [admin.user.weave_sub]} in _changes(db_session)


def test_an_admin_by_patch_role_only_goes_on_every_box(db_session, admin, weave, box) -> None:
    _, row, owner_key = box
    admin_key = _key(db_session, admin)
    weave.set_roles(admin.user.weave_sub, [], patches={"ganymede": ["krater:admin"]})

    added, _removed = box_access.sync_admins(db_session, weave, now=AT)

    assert added == [admin.user.weave_sub]
    assert _beat(db_session, row, 5).ssh_keys == (owner_key, admin_key)


def test_a_locked_admin_account_isnt_listed(db_session, admin, weave, box) -> None:
    _key(db_session, admin)
    weave.set_active(admin.user.weave_sub, False)

    assert box_access.sync_admins(db_session, weave, now=AT) == ([], [])
    assert box_access.admin_ids(db_session, now=AT) == []


def test_members_and_reviewers_arent_listed(db_session, member, make_actor, weave) -> None:
    make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))

    assert box_access.sync_admins(db_session, weave, now=AT) == ([], [])


def test_an_unchanged_list_writes_no_audit_event(db_session, admin, weave) -> None:
    box_access.sync_admins(db_session, weave, now=AT)
    box_access.sync_admins(db_session, weave, now=AT + timedelta(minutes=5))

    assert len(_changes(db_session)) == 1
    assert db_session.get(BoxAdmin, admin.user.id).synced_at == AT + timedelta(minutes=5)


def test_with_weave_down_the_last_list_counts_for_an_hour(db_session, admin, weave, box, monkeypatch) -> None:
    _, row, owner_key = box
    admin_key = _key(db_session, admin)
    box_access.sync_admins(db_session, weave, now=AT)

    def unavailable(role):
        raise WeaveUnavailableError("down")

    monkeypatch.setattr(weave, "list_users_with_role", unavailable)
    with pytest.raises(WeaveUnavailableError):
        box_access.sync_admins(db_session, weave, now=AT + timedelta(minutes=30))

    almost = AT + box_access.ROSTER_STALE - timedelta(minutes=1)
    assert runner_api.heartbeat(db_session, row, {"key_updates": True}, now=almost).ssh_keys == (owner_key, admin_key)
    stale = AT + box_access.ROSTER_STALE
    assert runner_api.heartbeat(db_session, row, {"key_updates": True}, now=stale).ssh_keys == (owner_key,)

    # Weave answers again: the admin is back, and the audit log says so.
    monkeypatch.undo()
    later = stale + timedelta(minutes=5)
    assert box_access.sync_admins(db_session, weave, now=later) == ([admin.user.weave_sub], [])
    assert runner_api.heartbeat(db_session, row, {"key_updates": True}, now=later).ssh_keys == (owner_key, admin_key)


def test_a_key_the_admin_removes_leaves_the_box(db_session, admin, weave, box) -> None:
    _, row, owner_key = box
    _key(db_session, admin)
    box_access.sync_admins(db_session, weave, now=AT)

    for key in ssh_keys.list_keys(db_session, admin.user):
        ssh_keys.remove_key(db_session, admin, key_id=key.id)

    assert _beat(db_session, row, 5).ssh_keys == (owner_key,)


def test_an_admin_who_owns_the_box_appears_once(db_session, admin, weave, project, client, fake) -> None:  # noqa: F811
    add_to_team(db_session, project, admin)
    own_key = _key(db_session, admin)
    job, row = _running_box(db_session, admin, project, client, fake)
    box_access.sync_admins(db_session, weave, now=AT)

    assert _beat(db_session, row, 5, key_updates=True).ssh_keys == (own_key,)


def test_a_batch_job_heartbeat_carries_no_keys(db_session, member, admin, weave, project, client, fake) -> None:  # noqa: F811
    from tests.compute.test_scheduler import _instance, _pass, _token

    _key(db_session, admin)
    box_access.sync_admins(db_session, weave, now=AT)
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    instance = runner_api.authenticate(db_session, _token(fake, row))

    assert runner_api.heartbeat(db_session, instance, {}, now=AT).ssh_keys is None


def test_the_heartbeat_keeps_the_runners_key_updates_flag(db_session, box) -> None:
    _, row, _ = box

    assert row.last_heartbeat["key_updates"] is True
    _beat(db_session, row, 5, key_updates="yes")  # wrong type: dropped
    assert "key_updates" not in row.last_heartbeat
