"""HTTP-level tests for POST /internal/skypilot/policy: the SkyPilot admin-policy launch gate.

No sign-in involved: this endpoint is called by SkyPilot itself (and, per docs/dev/skypilot-spike.md,
by members' own machines), never by a browser with a Krater session.
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import AuditEvent, BudgetEntryKind, Project, ProjectStatus
from krater.services import budget
from krater.skypilot_policy.envelope import decode_response
from tests.conftest import MEMBER_SUB
from tests.web.conftest import actor_for

POLICY_URL = "/internal/skypilot/policy"
TOKEN = "test-policy-token-0123456789abcdef"


def _wire_body(*, task: dict, config: dict, request_name: str = "launch", at_client_side: bool = True) -> bytes:
    inner = {
        "task": yaml.safe_dump(task, sort_keys=False),
        "skypilot_config": yaml.safe_dump(config, sort_keys=False),
        "request_name": request_name,
        "request_options": {"cluster_name": "test", "dryrun": True},
        "at_client_side": at_client_side,
        "user": "",
        "client_api_version": None,
        "client_version": None,
    }
    return json.dumps(json.dumps(inner)).encode()


def _post(client: TestClient, *, token: str | None = TOKEN, body: bytes | None = None, **body_kwargs):
    if body is None:
        body = _wire_body(task=body_kwargs.pop("task", {"resources": {"infra": "vast"}}), **body_kwargs)
    params = {} if token is None else {"token": token}
    return client.post(POLICY_URL, params=params, content=body, headers={"content-type": "application/json"})


def test_wrong_token_is_a_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)

    response = _post(client, token="not-the-token", config={"active_workspace": "default"})

    assert response.status_code == 404


def test_missing_token_is_a_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)

    response = _post(client, token=None, config={"active_workspace": "default"})

    assert response.status_code == 404


def test_unconfigured_token_always_404s(client: TestClient, monkeypatch) -> None:
    """An empty configured token (misconfiguration) must never accept an empty/absent token either."""
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", "")

    response = _post(client, token="", config={"active_workspace": "default"})

    assert response.status_code == 404


def test_malformed_body_is_a_400(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)

    response = _post(client, body=b"not a valid envelope at all")

    assert response.status_code == 400
    assert response.text  # a generic message, not a stack trace


def test_no_database_writes_happen(client: TestClient, monkeypatch, db_session: Session) -> None:
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)

    audit_before = db_session.scalar(sa.select(sa.func.count()).select_from(AuditEvent))
    project_before = db_session.scalar(sa.select(sa.func.count()).select_from(Project))

    # One reject, one allow, one malformed -- none of them should touch the database.
    _post(client, config={"active_workspace": "default"})
    _post(client, config={})
    _post(client, body=b"garbage")

    audit_after = db_session.scalar(sa.select(sa.func.count()).select_from(AuditEvent))
    project_after = db_session.scalar(sa.select(sa.func.count()).select_from(Project))

    assert audit_after == audit_before
    assert project_after == project_before


def test_reject_returns_400_with_the_message(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)

    response = _post(client, config={"active_workspace": "default"})

    assert response.status_code == 400
    assert "sky launch -w" in response.text


def test_allow_returns_a_body_shaped_like_the_accepted_fixture(
    client: TestClient, monkeypatch, db_session: Session, login_as
) -> None:
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)
    monkeypatch.setattr(get_settings(), "skypilot_autodown_idle_minutes", 30)
    monkeypatch.setattr(get_settings(), "skypilot_max_hourly_cost_cents", 500)

    member = login_as(MEMBER_SUB)
    project = Project(
        title="Route Test Project",
        submitter_id=member.id,
        status=ProjectStatus.APPROVED,
        skypilot_workspace="ganymede-route-test",
    )
    db_session.add(project)
    db_session.flush()
    budget.add_entry(
        db_session,
        project=project,
        kind=BudgetEntryKind.INITIAL_APPROVAL,
        amount_cents=100_000,
        actor=actor_for(member),
    )
    db_session.flush()

    response = _post(
        client,
        task={"resources": {"infra": "vast", "accelerators": {"A100": 1}}},
        config={"active_workspace": "ganymede-route-test"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    decoded = decode_response(response.text)
    assert decoded.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}
    assert decoded.task["resources"]["max_hourly_cost"] == 5.0


def test_a_reject_logs_who_and_where_in_the_message_itself(client: TestClient, monkeypatch, caplog) -> None:
    # Neither log format prints `extra=` fields, so the context has to be part of the message.
    monkeypatch.setattr(get_settings(), "skypilot_policy_token", TOKEN)
    # Alembic's `fileConfig` (run when the test database is migrated) disables loggers that already exist.
    monkeypatch.setattr(logging.getLogger("krater.web.routers.skypilot_policy"), "disabled", False)

    with caplog.at_level("WARNING", logger="krater.web.routers.skypilot_policy"):
        _post(client, config={"active_workspace": "ganymede-nope"}, request_name="jobs.launch")

    (record,) = [r for r in caplog.records if "skypilot launch blocked" in r.getMessage()]
    message = record.getMessage()
    assert "request=jobs.launch" in message
    assert "workspace=ganymede-nope" in message
    assert "at_client_side=True" in message
