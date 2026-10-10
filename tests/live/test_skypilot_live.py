"""End-to-end checks against a **real** SkyPilot 0.13.0 API server (and, for a couple of fail-closed
checks, a really-running Krater process) -- not the `httpx.MockTransport` fake in
`tests/skypilot/test_live_client.py`. Marked `@pytest.mark.live` (deselected by default; see
`pyproject.toml`'s `markers`).

This is the regression net for the wire-format bugs the initial spike/build missed and this contract
run found (see `docs/dev/skypilot-spike.md` and `docs/dev/skypilot-contract.md`): `StatusBody.refresh`
being an enum, not a bool, and `/jobs/queue`/`/jobs/cancel` raising `ClusterNotUpError` (HTTP 500, not a
clean `FAILED` poll body) when a workspace's managed-jobs controller doesn't exist yet. Both are things
`httpx.MockTransport` can't catch on its own, because the fake has to already know the real shape to
fake it -- these tests exist so a future SkyPilot upgrade that changes the wire format again fails here
first, against the real thing, not in production.

Run via `scripts/dev/skypilot_contract.sh`, which starts a real server, mints the tokens, and runs
`uv run pytest -m live tests/live/test_skypilot_live.py`. Running it directly needs the same env vars
that script sets -- see each skip message below, and `docs/dev/skypilot-contract.md`.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from krater.config import Settings
from krater.skypilot.live import LiveSkyPilotClient

pytestmark = pytest.mark.live

_API_URL = os.environ.get("SKYPILOT_LIVE_API_URL", "")
_SERVICE_TOKEN = os.environ.get("SKYPILOT_LIVE_SERVICE_TOKEN", "")
_KRATER_BASE_URL = os.environ.get("SKYPILOT_LIVE_KRATER_BASE_URL", "")
_POLICY_TOKEN = os.environ.get("SKYPILOT_LIVE_POLICY_TOKEN", "")

_MISSING_CLIENT_ENV = not (_API_URL and _SERVICE_TOKEN)
_MISSING_POLICY_ENV = not (_KRATER_BASE_URL and _POLICY_TOKEN)

skip_without_skypilot_server = pytest.mark.skipif(
    _MISSING_CLIENT_ENV,
    reason=(
        "needs a real running SkyPilot API server: set SKYPILOT_LIVE_API_URL and "
        "SKYPILOT_LIVE_SERVICE_TOKEN (an admin service-account bearer token) -- "
        "see scripts/dev/skypilot_contract.sh"
    ),
)
skip_without_krater_process = pytest.mark.skipif(
    _MISSING_POLICY_ENV,
    reason=(
        "needs a really-running Krater process (not the in-process TestClient) with "
        "KRATER_SKYPILOT_POLICY_TOKEN set: export SKYPILOT_LIVE_KRATER_BASE_URL and "
        "SKYPILOT_LIVE_POLICY_TOKEN -- see scripts/dev/skypilot_contract.sh"
    ),
)


@pytest.fixture
def live_client() -> LiveSkyPilotClient:
    settings = Settings(skypilot_mode="live", skypilot_api_url=_API_URL, skypilot_service_token=_SERVICE_TOKEN)
    return LiveSkyPilotClient(settings)


@pytest.fixture
def live_workspace(live_client: LiveSkyPilotClient):
    """A workspace name unique to this test run, deleted afterwards even if the test fails."""
    name = f"ganymede-livetest-{uuid.uuid4().hex[:12]}"
    yield name
    live_client.delete_workspace(name)


# --------------------------------------------------------------------------------------------------
# `LiveSkyPilotClient` contract: workspace lifecycle, allowed_users, Vast-only, and the read-side
# calls a project's reconciler makes -- all against the real server, over real REST.
# --------------------------------------------------------------------------------------------------


@skip_without_skypilot_server
def test_workspace_lifecycle_and_reconciler_reads_against_a_real_server(
    live_client: LiveSkyPilotClient, live_workspace: str
) -> None:
    live_client.create_workspace(live_workspace, allowed_users=["a@example.com", "b@example.com"])

    assert live_workspace in live_client.list_workspaces()

    # A team change is a full-list replace (per docs/skypilot-integration.md section 1), not an
    # add/remove -- confirm it actually lands on the real server.
    live_client.update_workspace(live_workspace, allowed_users=["only-this-one@example.com"])

    # Every read a reconcile pass makes, with no clusters ever launched in this brand-new workspace.
    # Each of these caught a real bug in this contract run (see the module docstring): `list_clusters`
    # 422ed on `refresh: false`, and `list_managed_jobs` raised on a workspace with no jobs-controller
    # cluster yet (the common case for a fresh workspace) instead of returning `[]`.
    assert live_client.cost_report(days=3650) == []
    assert live_client.list_clusters(live_workspace) == []
    assert live_client.list_managed_jobs(live_workspace) == []

    # Teardown calls `cancel_managed_jobs` unconditionally (docs/skypilot-integration.md section 1) --
    # must be a clean no-op here too, for the same "no jobs controller yet" reason.
    live_client.cancel_managed_jobs(live_workspace)

    live_client.delete_workspace(live_workspace)
    assert live_workspace not in live_client.list_workspaces()


# --------------------------------------------------------------------------------------------------
# Fail-closed checks against a really-running Krater process (not the in-process `TestClient`): these
# specifically prove the *deployed* process's settings and routing work, which an in-process test
# (tests/web/test_skypilot_policy.py) can't -- it never leaves the test process.
# --------------------------------------------------------------------------------------------------


def _wire_request(*, task: dict, config: dict, request_name: str = "launch") -> bytes:
    """Encode a `RestfulAdminPolicy`-shaped request body: double-JSON-encoded, with `task`/
    `skypilot_config` as embedded YAML strings (see docs/dev/skypilot-spike.md section 1 and
    krater/skypilot_policy/envelope.py). Avoids importing PyYAML here by keeping both fields
    JSON-parseable -- YAML is a superset of JSON, so this round-trips through `yaml.safe_load` fine.
    """
    inner = {
        "task": json.dumps(task),
        "skypilot_config": json.dumps(config),
        "request_name": request_name,
        "request_options": {},
        "at_client_side": True,
        "user": "",
    }
    return json.dumps(json.dumps(inner)).encode()


@skip_without_krater_process
def test_real_process_rejects_a_launch_with_no_workspace_selected() -> None:
    response = httpx.post(
        f"{_KRATER_BASE_URL}/internal/skypilot/policy",
        params={"token": _POLICY_TOKEN},
        content=_wire_request(task={"resources": {"infra": "vast"}}, config={}),
        headers={"content-type": "application/json"},
        timeout=10.0,
    )

    assert response.status_code == 400
    assert "No Ganymede project workspace selected" in response.text


@skip_without_krater_process
def test_real_process_rejects_the_default_workspace() -> None:
    response = httpx.post(
        f"{_KRATER_BASE_URL}/internal/skypilot/policy",
        params={"token": _POLICY_TOKEN},
        content=_wire_request(task={"resources": {"infra": "vast"}}, config={"active_workspace": "default"}),
        headers={"content-type": "application/json"},
        timeout=10.0,
    )

    assert response.status_code == 400
    assert "No Ganymede project workspace selected" in response.text


@skip_without_krater_process
def test_real_process_404s_on_a_wrong_token() -> None:
    # The route's own fail-closed contract (docs/skypilot-integration.md section 2): a wrong/missing
    # token gets a plain 404, not a 401/403, so the endpoint doesn't reveal its own existence to a
    # scanner. `sky launch`'s `RestfulAdminPolicy` turns any non-400 error status into a client-side
    # `RestfulPolicyError`, failing the launch closed -- confirmed manually against the real `sky` CLI
    # in this contract run (see docs/dev/skypilot-contract.md); this test covers the HTTP contract that
    # behavior depends on, without needing the `sky` CLI itself.
    response = httpx.post(
        f"{_KRATER_BASE_URL}/internal/skypilot/policy",
        params={"token": "not-the-real-token"},
        content=_wire_request(task={"resources": {"infra": "vast"}}, config={"active_workspace": "default"}),
        headers={"content-type": "application/json"},
        timeout=10.0,
    )

    assert response.status_code == 404
