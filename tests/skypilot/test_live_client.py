"""`LiveSkyPilotClient` against a fake SkyPilot API server (`httpx.MockTransport`): async request-id
polling, the FAILED/timeout/network-error error mapping, and cents conversion. Wire shapes taken from
`tests/fixtures/skypilot/` (see `docs/dev/skypilot-spike.md`)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
import pytest

from krater.config import Settings
from krater.skypilot.errors import (
    SkyPilotRequestFailedError,
    SkyPilotUnavailableError,
    SkyPilotWorkspaceNotFoundError,
)
from krater.skypilot.live import LiveSkyPilotClient

API_URL = "https://skypilot.test"
TOKEN = "sky_test_token"

#: Paths handled by the generic async-schedule-then-poll machinery below.
_SCHEDULED_PATHS = {
    "/workspaces/create",
    "/workspaces/update",
    "/workspaces/delete",
    "/workspaces",
    "/cost_report",
    "/status",
    "/jobs/queue",
    "/down",
    "/jobs/cancel",
    "/serve/status",
    "/serve/down",
}


def _settings() -> Settings:
    return Settings(skypilot_mode="live", skypilot_api_url=API_URL, skypilot_service_token=TOKEN)


class FakeSkyPilotServer:
    """A minimal fake of SkyPilot's REST surface, driven by an `httpx.MockTransport`.

    Every scheduled ("async") endpoint replies `200` + `null` body + an `x-skypilot-request-id`
    header, exactly like the spike found; the *next* `GET /api/get` for that id returns the queued
    poll response for that path (`self.poll_queue`), defaulting to an immediate `SUCCEEDED` with a
    `null` return value. `self.sync_error`, when set, makes every scheduled endpoint fail
    synchronously instead (no request id issued at all) -- the shape of the spike's
    `workspaces_update_forbidden_nonmember_response.json` fixture.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._request_id_seq = itertools.count(1)
        #: path -> list of `{"status": ..., "return_value": ..., "error": ...}` dicts to hand back in
        #: order, one per call to that path (repeats the last one once exhausted).
        self.poll_queue: dict[str, list[dict]] = {}
        self.sync_error: tuple[int, dict] | None = None
        #: request_id -> (source path, the poll response fixed at schedule time).
        self._pending: dict[str, tuple[str, dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/get":
            request_id = request.url.params["request_id"]
            source_path, poll_response = self._pending[request_id]
            del source_path
            # A request whose *handler* raised (rather than one that completed and rejected
            # something) comes back as HTTP 500 with the same `status`/`error` dict nested under
            # `detail`, not the flat 200 body every other FAILED/SUCCEEDED poll uses -- confirmed
            # live against a real 0.13.0 server (`/jobs/queue`, `/jobs/cancel` with no jobs
            # controller yet; see `docs/dev/skypilot-spike.md`).
            if poll_response.pop("_wrap_in_5xx_detail", False):
                return httpx.Response(500, json={"detail": {"request_id": request_id, **poll_response}})
            return httpx.Response(200, json={"request_id": request_id, **poll_response})

        if path not in _SCHEDULED_PATHS:
            return httpx.Response(404, json={"error": "not_found"})

        if self.sync_error is not None:
            status, body = self.sync_error
            return httpx.Response(status, json=body)

        queue = self.poll_queue.get(path, [{"status": "SUCCEEDED", "return_value": "null", "error": None}])
        poll_response = queue[0] if len(queue) == 1 else queue.pop(0)
        request_id = f"req-{next(self._request_id_seq)}"
        self._pending[request_id] = (path, poll_response)
        return httpx.Response(200, headers={"x-skypilot-request-id": request_id})


@pytest.fixture
def fake_server() -> FakeSkyPilotServer:
    return FakeSkyPilotServer()


@pytest.fixture
def client(fake_server: FakeSkyPilotServer) -> LiveSkyPilotClient:
    http_client = httpx.Client(transport=httpx.MockTransport(fake_server.handler))
    return LiveSkyPilotClient(
        _settings(), http_client=http_client, poll_timeout_seconds=0.2, poll_interval_seconds=0.02
    )


def test_create_workspace_sends_bearer_token_and_vast_only_config(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    client.create_workspace("ganymede-abc123", allowed_users=["a@x.com", "b@x.com"])

    request = next(r for r in fake_server.requests if r.url.path == "/workspaces/create")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["workspace_name"] == "ganymede-abc123"
    assert body["config"]["private"] is True
    assert body["config"]["allowed_users"] == ["a@x.com", "b@x.com"]
    # Every other cloud denied, per the spike's "no per-workspace allowlist" finding.
    assert body["config"]["aws"] == {"disabled": True}
    assert "vast" not in body["config"]


#: Every compute cloud in SkyPilot 0.13.0's `sky.utils.registry.CLOUD_REGISTRY`, checked against the staging server.
#: Update it (and the deny-lists) when SkyPilot is upgraded.
SKYPILOT_0_13_CLOUDS = {
    "aws", "azure", "cudo", "do", "fluidstack", "gcp", "hyperbolic", "ibm", "kubernetes", "lambda", "mithril",
    "nebius", "oci", "paperspace", "primeintellect", "runpod", "scp", "seeweb", "shadeform", "slurm", "ssh", "vast",
    "verda", "vsphere", "yotta",
}  # fmt: skip


def test_a_project_workspace_disables_every_cloud_but_vast(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    client.create_workspace("ganymede-abc123", allowed_users=["a@x.com"])

    request = next(r for r in fake_server.requests if r.url.path == "/workspaces/create")
    config = json.loads(request.content)["config"]
    disabled = {cloud for cloud, value in config.items() if value == {"disabled": True}}
    assert disabled == SKYPILOT_0_13_CLOUDS - {"vast"}


def test_the_default_workspace_in_compose_disables_every_cloud() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")

    missing = [cloud for cloud in sorted(SKYPILOT_0_13_CLOUDS) if f"{cloud}: {{disabled: true}}" not in compose]

    assert missing == []


def test_update_workspace_replaces_allowed_users(fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient) -> None:
    client.update_workspace("ganymede-abc123", allowed_users=["only-this-one@x.com"])

    request = next(r for r in fake_server.requests if r.url.path == "/workspaces/update")
    body = json.loads(request.content)
    assert body["config"]["allowed_users"] == ["only-this-one@x.com"]


def test_delete_workspace(fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient) -> None:
    client.delete_workspace("ganymede-abc123")

    request = next(r for r in fake_server.requests if r.url.path == "/workspaces/delete")
    assert json.loads(request.content) == {"workspace_name": "ganymede-abc123"}


def test_delete_workspace_is_a_no_op_when_already_gone(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    # Confirmed live against a real 0.13.0 server: deleting a workspace that doesn't exist is a polled
    # `FAILED` request, not a no-op -- the `SkyPilotClient` protocol's "safe to call on a workspace
    # that's already gone" promise (which `sync_workspaces` relies on) has to be implemented here.
    fake_server.poll_queue["/workspaces/delete"] = [
        {"status": "FAILED", "return_value": None, "error": "Workspace 'ganymede-x' does not exist."}
    ]

    client.delete_workspace("ganymede-x")  # must not raise


def test_delete_workspace_still_raises_for_a_different_failure(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/workspaces/delete"] = [
        {"status": "FAILED", "return_value": None, "error": "permission denied"}
    ]

    with pytest.raises(SkyPilotRequestFailedError, match="permission denied"):
        client.delete_workspace("ganymede-x")


def test_list_workspaces_decodes_the_returned_mapping(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/workspaces"] = [
        {
            "status": "SUCCEEDED",
            "return_value": {"ganymede-priv-test": {"private": True, "allowed_users": ["54400d50"]}, "default": {}},
            "error": None,
        }
    ]

    names = client.list_workspaces()

    assert sorted(names) == ["default", "ganymede-priv-test"]


def test_cost_report_converts_dollars_to_cents_and_groups_by_workspace(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    # `return_value` is itself a JSON-encoded string in the real spike fixture
    # (`tests/fixtures/skypilot/cost_report_request_response.json`), not a bare JSON array.
    fake_server.poll_queue["/cost_report"] = [
        {
            "status": "SUCCEEDED",
            "return_value": json.dumps(
                [
                    {"name": "cluster-a", "workspace": "ganymede-abc123", "total_cost": 12.34},
                    {"name": "cluster-b", "workspace": "ganymede-abc123", "total_cost": 0.5},
                    {"name": "controller", "workspace": "default", "total_cost": 1.0},
                ]
            ),
            "error": None,
        }
    ]

    rows = client.cost_report(days=30)

    request = next(r for r in fake_server.requests if r.url.path == "/cost_report")
    assert json.loads(request.content) == {"days": 30}
    by_cluster = {row.cluster_name: (row.workspace, row.total_cost_cents) for row in rows}
    assert by_cluster["cluster-a"] == ("ganymede-abc123", 1234)
    assert by_cluster["cluster-b"] == ("ganymede-abc123", 50)
    assert by_cluster["controller"] == ("default", 100)


def test_cost_report_handles_an_empty_report(fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient) -> None:
    fake_server.poll_queue["/cost_report"] = [{"status": "SUCCEEDED", "return_value": "[]", "error": None}]

    assert client.cost_report(days=30) == []


def test_list_clusters_filters_to_the_requested_workspace(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/status"] = [
        {
            "status": "SUCCEEDED",
            "return_value": json.dumps(
                [
                    {"name": "in-workspace", "workspace": "ganymede-abc123", "status": "UP"},
                    {"name": "other-workspace", "workspace": "ganymede-other", "status": "UP"},
                ]
            ),
            "error": None,
        }
    ]

    clusters = client.list_clusters("ganymede-abc123")

    assert [c.name for c in clusters] == ["in-workspace"]
    request = next(r for r in fake_server.requests if r.url.path == "/status")
    body = json.loads(request.content)
    assert body["override_skypilot_config"]["active_workspace"] == "ganymede-abc123"
    # `StatusBody.refresh` is a `StatusRefreshMode` enum ("NONE"/"AUTO"/"FORCE"), not a bool -- a real
    # 0.13.0 server 422s on `false` (confirmed live; see docs/dev/skypilot-spike.md).
    assert body["refresh"] == "NONE"


def test_list_managed_jobs_filters_to_the_requested_workspace(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/jobs/queue"] = [
        {
            "status": "SUCCEEDED",
            "return_value": json.dumps(
                [
                    {"job_id": 1, "job_name": "train", "workspace": "ganymede-abc123", "status": "RUNNING"},
                    {"job_id": 2, "job_name": "other", "workspace": "ganymede-other", "status": "RUNNING"},
                ]
            ),
            "error": None,
        }
    ]

    jobs = client.list_managed_jobs("ganymede-abc123")

    assert [(j.job_id, j.name) for j in jobs] == [(1, "train")]


def test_down_cluster_sends_purge(fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient) -> None:
    client.down_cluster("some-cluster")

    request = next(r for r in fake_server.requests if r.url.path == "/down")
    body = json.loads(request.content)
    assert body["cluster_name"] == "some-cluster"
    assert body["purge"] is True


def test_cancel_managed_jobs_scopes_to_the_workspace(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    client.cancel_managed_jobs("ganymede-abc123")

    request = next(r for r in fake_server.requests if r.url.path == "/jobs/cancel")
    body = json.loads(request.content)
    assert body["all"] is True
    assert body["override_skypilot_config"]["active_workspace"] == "ganymede-abc123"


def test_a_failed_poll_raises_request_failed_with_the_servers_message(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/workspaces/create"] = [
        {"status": "FAILED", "return_value": None, "error": "boom: traceback"}
    ]

    with pytest.raises(SkyPilotRequestFailedError, match="boom"):
        client.create_workspace("ganymede-x", allowed_users=[])


def test_a_synchronous_error_response_raises_request_failed_with_the_servers_message(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    # No x-skypilot-request-id at all -- exactly the shape of the spike's
    # `workspaces_update_forbidden_nonmember_response.json` fixture (a 403 with a plain `detail`).
    fake_server.sync_error = (403, {"detail": "Forbidden"})

    with pytest.raises(SkyPilotRequestFailedError, match="Forbidden"):
        client.update_workspace("ganymede-x", allowed_users=["nonmember@x.com"])


def test_a_5xx_response_raises_unavailable(fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient) -> None:
    fake_server.sync_error = (503, {"detail": "starting up"})

    with pytest.raises(SkyPilotUnavailableError):
        client.create_workspace("ganymede-x", allowed_users=[])


def test_a_network_error_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = LiveSkyPilotClient(_settings(), http_client=http_client)

    with pytest.raises(SkyPilotUnavailableError):
        client.create_workspace("ganymede-x", allowed_users=[])


def test_a_request_that_never_completes_times_out_as_unavailable(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/workspaces/create"] = [{"status": "RUNNING", "return_value": None, "error": None}]

    with pytest.raises(SkyPilotUnavailableError, match="did not complete"):
        client.create_workspace("ganymede-x", allowed_users=[])


# --------------------------------------------------------------------------------------------------
# "No jobs controller yet" (`sky.exceptions.ClusterNotUpError`) -- confirmed live against a real
# 0.13.0 server: `/jobs/queue` and `/jobs/cancel` both raise this when a workspace's managed-jobs
# controller cluster doesn't exist yet (i.e. no managed job has ever been launched there), which is
# the common case for essentially every Ganymede workspace. See docs/dev/skypilot-spike.md.
# --------------------------------------------------------------------------------------------------

_NO_JOBS_CONTROLLER_POLL_RESPONSE = {
    "status": "FAILED",
    "return_value": "null",
    "error": json.dumps({"type": "ClusterNotUpError", "message": "No in-progress managed jobs."}),
    "_wrap_in_5xx_detail": True,
}


def test_list_managed_jobs_treats_no_jobs_controller_as_empty(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/jobs/queue"] = [dict(_NO_JOBS_CONTROLLER_POLL_RESPONSE)]

    assert client.list_managed_jobs("ganymede-abc123") == []


def test_cancel_managed_jobs_treats_no_jobs_controller_as_a_no_op(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/jobs/cancel"] = [dict(_NO_JOBS_CONTROLLER_POLL_RESPONSE)]

    client.cancel_managed_jobs("ganymede-abc123")  # must not raise


def test_list_managed_jobs_still_raises_for_a_different_5xx_wrapped_failure(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/jobs/queue"] = [
        {
            "status": "FAILED",
            "return_value": "null",
            "error": json.dumps({"type": "RuntimeError", "message": "something else broke"}),
            "_wrap_in_5xx_detail": True,
        }
    ]

    with pytest.raises(SkyPilotRequestFailedError, match="something else broke"):
        client.list_managed_jobs("ganymede-abc123")


def test_cancel_managed_jobs_still_raises_for_a_different_5xx_wrapped_failure(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/jobs/cancel"] = [
        {
            "status": "FAILED",
            "return_value": "null",
            "error": json.dumps({"type": "RuntimeError", "message": "something else broke"}),
            "_wrap_in_5xx_detail": True,
        }
    ]

    with pytest.raises(SkyPilotRequestFailedError, match="something else broke"):
        client.cancel_managed_jobs("ganymede-abc123")


# --------------------------------------------------------------------------------------------------
# "No serve controller yet" -- confirmed live against a real 0.13.0 server with internet access (the
# SkyPilot contract run): `/serve/status` raises `ClusterNotUpError("No live services.")`, as a 500-
# wrapped FAILED poll, in a workspace that never ran `sky serve up`. The message doesn't name the type,
# so the client must match on the polled error's `type`, or every completed/withdrawn project's
# teardown fails and its workspace is never deleted.
# --------------------------------------------------------------------------------------------------

_NO_SERVE_CONTROLLER_POLL_RESPONSE = {
    "status": "FAILED",
    "return_value": "null",
    "error": json.dumps({"type": "ClusterNotUpError", "message": "No live services."}),
    "_wrap_in_5xx_detail": True,
}


def test_list_services_treats_no_serve_controller_as_empty(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/serve/status"] = [dict(_NO_SERVE_CONTROLLER_POLL_RESPONSE)]

    assert client.list_services("ganymede-abc123") == []


def test_down_service_treats_no_serve_controller_as_a_no_op(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/serve/down"] = [dict(_NO_SERVE_CONTROLLER_POLL_RESPONSE)]

    client.down_service("svc")  # must not raise


def test_list_services_still_raises_for_a_different_failure(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/serve/status"] = [
        {
            "status": "FAILED",
            "return_value": "null",
            "error": json.dumps(
                {"type": "NetworkError", "message": "Failed to refresh services status due to network error"}
            ),
            "_wrap_in_5xx_detail": True,
        }
    ]

    with pytest.raises(SkyPilotRequestFailedError, match="network error") as excinfo:
        client.list_services("ganymede-abc123")
    assert excinfo.value.error_type == "NetworkError"


# --------------------------------------------------------------------------------------------------
# A workspace that doesn't exist -- shapes captured live from a real 0.13.0 server: every call scoped
# to it is a 500-wrapped FAILED poll with a bare `ValueError`, so both the type and the message count.
# --------------------------------------------------------------------------------------------------


def _failed_poll(error_type: str, message: str) -> dict:
    return {
        "status": "FAILED",
        "return_value": "null",
        "error": json.dumps({"type": error_type, "message": message}),
        "_wrap_in_5xx_detail": True,
    }


_SCOPED_MISSING_MESSAGE = (
    "Workspace ganymede-abc123 does not exist. Use `sky check` to see if it is defined on the API server and try again."
)


@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("/status", lambda c: c.list_clusters("ganymede-abc123")),
        ("/jobs/queue", lambda c: c.list_managed_jobs("ganymede-abc123")),
        ("/jobs/cancel", lambda c: c.cancel_managed_jobs("ganymede-abc123")),
        ("/serve/status", lambda c: c.list_services("ganymede-abc123")),
    ],
)
def test_a_call_scoped_to_a_missing_workspace_raises_workspace_not_found(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient, path: str, call
) -> None:
    fake_server.poll_queue[path] = [_failed_poll("ValueError", _SCOPED_MISSING_MESSAGE)]

    with pytest.raises(SkyPilotWorkspaceNotFoundError, match="does not exist") as excinfo:
        call(client)
    assert excinfo.value.error_type == "ValueError"


def test_delete_workspace_is_a_no_op_for_the_real_missing_workspace_shape(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient
) -> None:
    fake_server.poll_queue["/workspaces/delete"] = [
        _failed_poll("ValueError", "Workspace 'ganymede-abc123' does not exist.")
    ]

    client.delete_workspace("ganymede-abc123")  # must not raise


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        ("ValueError", "Invalid cluster name: does not exist."),
        ("RuntimeError", _SCOPED_MISSING_MESSAGE),
    ],
)
def test_other_failures_are_not_mistaken_for_a_missing_workspace(
    fake_server: FakeSkyPilotServer, client: LiveSkyPilotClient, error_type: str, message: str
) -> None:
    fake_server.poll_queue["/status"] = [_failed_poll(error_type, message)]

    with pytest.raises(SkyPilotRequestFailedError) as excinfo:
        client.list_clusters("ganymede-abc123")
    assert not isinstance(excinfo.value, SkyPilotWorkspaceNotFoundError)
