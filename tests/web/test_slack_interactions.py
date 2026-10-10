"""HTTP-level tests for POST /slack/interactions: signature verification, and that the route acks fast
by deferring the real work (Approve/reject-modal-submit) to a job, opening the reject modal itself
being the one thing done synchronously (its `trigger_id` is single-use and expires in ~3s).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlencode

from fastapi.testclient import TestClient

import krater.web.routers.slack_interactions as route_module
from krater.config import get_settings
from krater.services import slack_reviews
from krater.slack import get_slack_client

SECRET = "test-interactions-signing-secret"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    base_string = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _post(client: TestClient, payload: dict, *, secret: str = SECRET, timestamp: str | None = None):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = timestamp or str(int(time.time()))
    signature = _sign(secret, timestamp, body)
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )


def _block_action_payload(*, action_id: str, revision_id: str, slack_user_id: str = "U_CLICKER") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": slack_user_id},
        "response_url": "https://hooks.slack.example/response/1",
        "trigger_id": "trigger.123",
        "actions": [{"action_id": action_id, "value": revision_id}],
    }


def test_bad_signature_is_rejected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "slack_signing_secret", SECRET)

    response = _post(
        client, _block_action_payload(action_id="approve", revision_id=str(uuid.uuid4())), secret="wrong-secret"
    )

    assert response.status_code == 401


def test_missing_signing_secret_rejects_everything(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "slack_signing_secret", "")

    response = _post(client, _block_action_payload(action_id="approve", revision_id=str(uuid.uuid4())))

    assert response.status_code == 401


def test_approve_click_acks_fast_and_defers_the_real_work(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "slack_signing_secret", SECRET)
    revision_id = str(uuid.uuid4())
    deferred: dict = {}
    monkeypatch.setattr(route_module.slack_process_approve, "defer", lambda **kwargs: deferred.update(kwargs) or 1)

    response = _post(
        client, _block_action_payload(action_id="approve", revision_id=revision_id, slack_user_id="U_REVIEWER")
    )

    assert response.status_code == 200
    assert deferred == {
        "revision_id": revision_id,
        "slack_user_id": "U_REVIEWER",
        "response_url": "https://hooks.slack.example/response/1",
    }


def test_reject_click_opens_the_modal_synchronously(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "slack_signing_secret", SECRET)
    revision_id = str(uuid.uuid4())

    response = _post(client, _block_action_payload(action_id="reject", revision_id=revision_id))

    assert response.status_code == 200
    slack_client = get_slack_client()
    assert len(slack_client.opened_views) == 1
    opened = slack_client.opened_views[-1]
    assert opened["trigger_id"] == "trigger.123"
    assert opened["view"]["callback_id"] == slack_reviews.REJECT_MODAL_CALLBACK_ID
    metadata = json.loads(opened["view"]["private_metadata"])
    assert metadata["revision_id"] == revision_id


def test_reject_modal_submission_defers_the_reject_with_its_reason(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "slack_signing_secret", SECRET)
    revision_id = str(uuid.uuid4())
    deferred: dict = {}
    monkeypatch.setattr(route_module.slack_process_reject, "defer", lambda **kwargs: deferred.update(kwargs) or 1)

    view = slack_reviews.reject_modal_view(
        revision_id=revision_id, response_url="https://hooks.slack.example/response/2"
    )
    view["state"] = {"values": {"reason_block": {"reason_input": {"type": "plain_text_input", "value": "Needs work."}}}}
    payload = {"type": "view_submission", "user": {"id": "U_REVIEWER"}, "view": view}

    response = _post(client, payload)

    assert response.status_code == 200
    assert deferred == {
        "revision_id": revision_id,
        "slack_user_id": "U_REVIEWER",
        "reason": "Needs work.",
        "response_url": "https://hooks.slack.example/response/2",
    }
