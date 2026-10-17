"""Project secrets: write-only values, encrypted at rest, delivered only through the runner's start call."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import AuditEvent, ProjectSecret
from krater.services import project_secrets
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from tests.compute.test_scheduler import NOW, _instance, _pass, _submit, _token
from tests.services.conftest import reviewer  # noqa: F401

KEY = Fernet.generate_key().decode()


@pytest.fixture
def settings():
    return get_settings().model_copy(update={"project_secrets_key": KEY, "project_secrets_key_file": ""})


def test_a_secret_is_stored_encrypted_and_audited_by_name_only(db_session, member, project, settings) -> None:
    project_secrets.set_secret(
        db_session, member, project=project, name="HF_TOKEN", value="hf_abc123", settings=settings
    )

    row = db_session.scalars(sa.select(ProjectSecret)).one()
    assert "hf_abc123" not in row.ciphertext
    assert project_secrets.environment(db_session, project.id, settings) == {"HF_TOKEN": "hf_abc123"}
    event = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == "project_secret.added")).one()
    assert event.payload == {"name": "HF_TOKEN"}


def test_setting_an_existing_name_replaces_its_value(db_session, member, project, settings) -> None:
    project_secrets.set_secret(
        db_session, member, project=project, name="WANDB_API_KEY", value="old", settings=settings
    )
    project_secrets.set_secret(
        db_session, member, project=project, name="WANDB_API_KEY", value="new", settings=settings
    )

    assert project_secrets.environment(db_session, project.id, settings) == {"WANDB_API_KEY": "new"}
    assert len(project_secrets.list_secrets(db_session, project)) == 1


@pytest.mark.parametrize(
    "name",
    ["hf_token", "1TOKEN", "HF-TOKEN", "KRATER_JOB_TOKEN", "VAST_TCP_PORT_22", "PATH", "LD_PRELOAD", "PYTHONPATH"],
)
def test_bad_or_reserved_names_are_refused(db_session, member, project, settings, name) -> None:
    with pytest.raises(ValidationFailed):
        project_secrets.set_secret(db_session, member, project=project, name=name, value="x", settings=settings)


def test_values_are_limited(db_session, member, project, settings) -> None:
    with pytest.raises(ValidationFailed):
        project_secrets.set_secret(db_session, member, project=project, name="BIG", value="x" * 8193, settings=settings)
    with pytest.raises(ValidationFailed):
        project_secrets.set_secret(db_session, member, project=project, name="EMPTY", value="", settings=settings)


def test_a_project_has_at_most_twenty(db_session, member, project, settings) -> None:
    for i in range(project_secrets.MAX_SECRETS_PER_PROJECT):
        project_secrets.set_secret(db_session, member, project=project, name=f"S{i}", value="v", settings=settings)

    with pytest.raises(ValidationFailed):
        project_secrets.set_secret(db_session, member, project=project, name="ONE_MORE", value="v", settings=settings)


def test_only_the_team_or_an_admin_can_change_them(db_session, member, admin, reviewer, project, settings) -> None:  # noqa: F811
    with pytest.raises(NotAllowed):
        project_secrets.set_secret(db_session, reviewer, project=project, name="HF_TOKEN", value="v", settings=settings)
    project_secrets.set_secret(db_session, admin, project=project, name="HF_TOKEN", value="v", settings=settings)
    with pytest.raises(NotAllowed):
        project_secrets.delete_secret(db_session, reviewer, project=project, name="HF_TOKEN")
    project_secrets.delete_secret(db_session, member, project=project, name="HF_TOKEN")

    assert project_secrets.list_secrets(db_session, project) == []


def test_without_a_key_the_feature_is_off(db_session, member, project, settings) -> None:
    off = settings.model_copy(update={"project_secrets_key": ""})

    assert not project_secrets.enabled(off)
    with pytest.raises(InvalidState):
        project_secrets.set_secret(db_session, member, project=project, name="HF_TOKEN", value="v", settings=off)


def test_values_survive_a_key_rotation(db_session, member, project, settings) -> None:
    project_secrets.set_secret(db_session, member, project=project, name="HF_TOKEN", value="v1", settings=settings)
    rotated = settings.model_copy(update={"project_secrets_key": f"{Fernet.generate_key().decode()},{KEY}"})

    assert project_secrets.environment(db_session, project.id, rotated) == {"HF_TOKEN": "v1"}


def test_a_value_whose_key_is_gone_is_left_out(db_session, member, project, settings) -> None:
    project_secrets.set_secret(db_session, member, project=project, name="HF_TOKEN", value="v1", settings=settings)
    other = settings.model_copy(update={"project_secrets_key": Fernet.generate_key().decode()})

    assert project_secrets.environment(db_session, project.id, other) == {}


def test_the_key_can_come_from_a_file(tmp_path, settings) -> None:
    key_file = tmp_path / "keys"
    key_file.write_text(f"# newest first\n{KEY}\n", encoding="utf-8")

    assert project_secrets.enabled(
        settings.model_copy(update={"project_secrets_key": "", "project_secrets_key_file": str(key_file)})
    )


def test_the_runner_gets_them_at_start_and_vast_never_does(db_session, member, project, client, fake, settings) -> None:
    project_secrets.set_secret(
        db_session, member, project=project, name="HF_TOKEN", value="hf_abc123", settings=settings
    )
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)

    create_body = next(iter(fake.instances.values()))
    assert "hf_abc123" not in str(create_body)  # not in the create request's env, onstart or args

    info = runner_api.start(
        db_session, runner_api.authenticate(db_session, _token(fake, row)), now=NOW, settings=settings
    )

    assert info.secrets == {"HF_TOKEN": "hf_abc123"}
    assert "hf_abc123" not in repr(info)


def test_rotation_re_encrypts_with_the_newest_key_so_the_old_one_can_go(db_session, member, project, settings) -> None:
    project_secrets.set_secret(db_session, member, project=project, name="HF_TOKEN", value="v1", settings=settings)
    new_key = Fernet.generate_key().decode()
    both = settings.model_copy(update={"project_secrets_key": f"{new_key},{KEY}"})

    assert project_secrets.rotate_all(db_session, both) == (1, [])

    only_new = settings.model_copy(update={"project_secrets_key": new_key})
    assert project_secrets.environment(db_session, project.id, only_new) == {"HF_TOKEN": "v1"}
