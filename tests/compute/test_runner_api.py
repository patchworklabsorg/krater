"""The runner's HTTP API (`/runner/v1/...`): token auth, start, heartbeat, exit, and checkpoint URLs."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from krater import providers
from krater.compute import runner_api
from krater.config import get_settings
from krater.db import get_session
from krater.models import ComputeJobKind, ComputeJobState
from krater.services import compute_jobs
from krater.storage import FakeObjectStore
from krater.web.app import create_app
from tests.compute.test_scheduler import NOW, _instance, _pass, _submit, _token


@pytest.fixture
def store(monkeypatch) -> FakeObjectStore:
    store = FakeObjectStore()
    monkeypatch.setattr("krater.web.routers.runner.get_compute_store", lambda: store)
    return store


@pytest.fixture
def http(db_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def machine(db_session, member, project, client, fake):  # noqa: F811
    """A rented, running machine: (job, instance row, its runner token)."""
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))
    return job, row, _token(fake, row)


@pytest.fixture
def vast_mode(monkeypatch) -> None:
    """The active provider is Vast, as in production (the tests' default is compute off)."""
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_start_tells_the_runner_its_command_and_whether_it_resumes(http, machine) -> None:
    job, row, token = machine

    first = http.post("/runner/v1/start", headers=_auth(token))
    second = http.post("/runner/v1/start", headers=_auth(token))

    assert first.status_code == 200
    assert first.json()["command"] == "python train.py"
    assert first.json()["resumed"] is False
    assert first.json()["job_id"] == str(job.id)
    assert second.json()["resumed"] is True  # the container restarted on the same machine
    assert row.runner_starts == 2


def test_start_tells_the_runner_when_to_stop_its_machine_without_krater(http, machine, vast_mode) -> None:
    _, row, token = machine

    info = http.post("/runner/v1/start", headers=_auth(token)).json()

    assert info["self_stop_after_seconds"] == 180 * 60
    assert info["vast_instance_id"] == int(row.provider_instance_id)  # the wire name the runner reads
    assert info["vast_api_url"] == "https://console.vast.ai"
    # The provider-neutral descriptor, with the id as a string; the legacy fields above stay for deployed runners.
    assert info["self_stop"] == {
        "method": "vast_api",
        "url": "https://console.vast.ai",
        "instance_id": row.provider_instance_id,
    }
    assert isinstance(info["self_stop"]["instance_id"], str)


def test_a_provider_that_cannot_stop_itself_sends_no_self_stop(http, machine, monkeypatch, vast_mode) -> None:
    _, _, token = machine
    vast = providers.registered()["vast"]
    monkeypatch.setitem(
        providers._REGISTRY,
        "vast",
        dataclasses.replace(
            vast, self_stop_method=None, capabilities=dataclasses.replace(vast.capabilities, self_stop=False)
        ),
    )

    info = http.post("/runner/v1/start", headers=_auth(token)).json()

    assert info["self_stop"] is None
    assert info["self_stop_after_seconds"] == -1


def test_a_shutdown_provider_sends_the_shutdown_method(http, machine, monkeypatch, vast_mode) -> None:
    _, _, token = machine
    vast = providers.registered()["vast"]
    monkeypatch.setitem(providers._REGISTRY, "vast", dataclasses.replace(vast, self_stop_method="shutdown"))

    info = http.post("/runner/v1/start", headers=_auth(token)).json()

    assert info["self_stop"] == {"method": "shutdown"}
    assert info["self_stop_after_seconds"] == 180 * 60


def test_a_wrong_or_missing_token_is_refused(http, machine) -> None:
    assert http.post("/runner/v1/start").status_code == 403
    assert http.post("/runner/v1/start", headers=_auth("not-a-token")).status_code == 403
    # Never from a query string.
    _, _, token = machine
    assert http.post(f"/runner/v1/start?token={token}").status_code == 403


def test_heartbeat_records_and_says_when_to_stop(http, machine, db_session, member) -> None:  # noqa: F811
    job, row, token = machine

    response = http.post("/runner/v1/heartbeat", headers=_auth(token), json={"gpu_util": 97.5, "sessions": 1, "x": 1})
    assert response.json() == {"stop": False, "sync": False}
    assert row.last_heartbeat == {"gpu_util": 97.5, "sessions": 1}

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    assert http.post("/runner/v1/heartbeat", headers=_auth(token), json={}).json() == {"stop": True, "sync": False}


def test_exit_finishes_the_job_once(http, machine) -> None:
    job, row, token = machine

    assert http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": 0}).json() == {"state": "succeeded"}
    again = http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": 3})

    assert again.json() == {"state": "succeeded"}
    assert job.state is ComputeJobState.SUCCEEDED and job.exit_code == 0
    assert http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": "x"}).status_code == 422


def test_a_failing_command_fails_the_job_and_the_machine_is_destroyed(http, machine, db_session, client, fake) -> None:  # noqa: F811
    job, row, token = machine

    http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": 2})
    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert job.state is ComputeJobState.FAILED and "code 2" in job.state_reason
    assert fake.instances == {}
    # The destroyed machine's token is dead.
    assert http.post("/runner/v1/heartbeat", headers=_auth(token), json={}).status_code == 403


def test_small_uploads_get_one_url_and_big_ones_get_parts(http, machine, store) -> None:
    job, row, token = machine

    small = http.post("/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": "s.pt", "size": 10})
    big = http.post(
        "/runner/v1/files/checkpoints/upload",
        headers=_auth(token),
        json={"name": "dir/big.pt", "size": runner_api.PART_BYTES * 2 + 1},
    )

    prefix = f"projects/{job.project_id}/jobs/{job.id}/checkpoints/"
    assert small.json()["url"].startswith(f"fake://krater-storage/{prefix}s.pt?put&length=10&")
    assert len(big.json()["parts"]) == 3
    # Each part URL is signed for its own size; the last one is the remainder.
    assert f"length={runner_api.PART_BYTES}&" in big.json()["parts"][0]
    assert "partNumber=3&length=1&" in big.json()["parts"][2]
    assert big.json()["part_bytes"] == runner_api.PART_BYTES
    done = http.post(
        "/runner/v1/files/checkpoints/complete",
        headers=_auth(token),
        json={"name": "dir/big.pt", "upload_id": big.json()["upload_id"], "etags": ['"a"', '"b"', '"c"']},
    )
    assert done.json() == {"ok": True}
    listed = http.post("/runner/v1/files/checkpoints/list", headers=_auth(token)).json()["files"]
    assert [f["name"] for f in listed] == ["dir/big.pt"]


def test_a_job_only_sees_its_own_files(http, machine, store) -> None:
    job, row, token = machine
    store.put("projects/other/jobs/other/checkpoints/x.pt", content_type="application/octet-stream", size_bytes=1)
    store.put(
        f"projects/{job.project_id}/jobs/{job.id}/outputs/result.json", content_type="application/json", size_bytes=2
    )

    assert http.post("/runner/v1/files/checkpoints/list", headers=_auth(token)).json() == {"files": []}
    outputs = http.post("/runner/v1/files/outputs/list", headers=_auth(token)).json()["files"]
    assert [(f["name"], f["size"]) for f in outputs] == [("result.json", 2)]


@pytest.mark.parametrize(
    "name", ["../x", "a/../../x", "/abs", "a//b", "dir/", "", ".", "a/./b", "x/.", "a\b", " lead", "tab	x", "é.pt"]
)
def test_upload_names_must_stay_inside_the_jobs_prefix(http, machine, store, name) -> None:
    _, _, token = machine

    response = http.post("/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": name, "size": 1})

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["epoch=3-step=100.ckpt", "run 2/model.pt", "a+b,c@d~e.bin"])
def test_names_ml_tools_write_are_accepted(http, machine, store, name) -> None:
    _, _, token = machine

    response = http.post("/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": name, "size": 1})

    assert response.status_code == 200, response.text


def test_unknown_areas_are_refused(http, machine, store) -> None:
    _, _, token = machine

    assert http.post("/runner/v1/files/secrets/list", headers=_auth(token)).status_code == 422


def test_the_runner_binary_is_served_when_built(http, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_runner_binary_path", str(tmp_path / "missing"))
    assert http.get("/runner/v1/krater-runner").status_code == 404

    binary = tmp_path / "krater-runner-linux-amd64"
    binary.write_bytes(b"\x7fELF fake")
    monkeypatch.setattr(get_settings(), "compute_runner_binary_path", str(binary))
    response = http.get("/runner/v1/krater-runner")

    assert response.status_code == 200 and response.content == b"\x7fELF fake"


def test_runner_sha256_follows_the_file(tmp_path) -> None:
    from krater.compute import launch

    binary = tmp_path / "runner"
    assert launch.runner_sha256(str(binary)) is None
    binary.write_bytes(b"one")
    first = launch.runner_sha256(str(binary))
    binary.write_bytes(b"two!")
    assert launch.runner_sha256(str(binary)) != first
    assert len(first) == 64


def test_uploads_must_fit_the_projects_storage_quota(http, machine, store, db_session, admin) -> None:  # noqa: F811
    from krater.services import compute_settings

    job, row, token = machine
    compute_settings.set_project_overrides(
        db_session, admin, project=job.project, overrides={"storage_quota_gb": 1}, reason="small"
    )
    gb = 1024**3
    prefix = f"projects/{job.project_id}/jobs/{job.id}/checkpoints/"
    store.put(prefix + "old.pt", content_type="application/octet-stream", size_bytes=gb - 100)

    too_big = http.post(
        "/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": "new.pt", "size": 101}
    )
    replacing = http.post(
        "/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": "old.pt", "size": gb}
    )

    assert too_big.status_code == 422 and "quota" in too_big.json()["error"]
    assert replacing.status_code == 200  # replacing a file counts only the difference


def test_uploads_in_flight_count_against_the_quota(http, machine, store, db_session, admin) -> None:  # noqa: F811
    from krater.services import compute_settings

    job, row, token = machine
    compute_settings.set_project_overrides(
        db_session, admin, project=job.project, overrides={"storage_quota_gb": 1}, reason="small"
    )
    db_session.commit()
    gb = 1024**3

    def begin(name: str, size: int):
        return http.post("/runner/v1/files/outputs/upload", headers=_auth(token), json={"name": name, "size": size})

    # Neither file is in storage yet: the second must still not get the space the first was promised.
    assert begin("a.bin", gb // 2 + 1).status_code == 200
    second = begin("b.bin", gb // 2)
    assert second.status_code == 422 and "quota" in second.json()["error"]
    # Asking again for the same file replaces its reservation rather than adding to it.
    assert begin("a.bin", gb // 2).status_code == 200
    # Once the first upload lands, it counts once, not as stored plus reserved.
    prefix = f"projects/{job.project_id}/jobs/{job.id}/outputs/"
    store.put(prefix + "a.bin", content_type="application/octet-stream", size_bytes=gb // 2)
    assert begin("b.bin", gb // 2).status_code == 200


def test_an_expired_reservation_frees_its_space(http, machine, store, db_session, admin, monkeypatch) -> None:  # noqa: F811
    from krater.services import compute_settings
    from krater.web.routers import runner as runner_routes

    job, row, token = machine
    compute_settings.set_project_overrides(
        db_session, admin, project=job.project, overrides={"storage_quota_gb": 1}, reason="small"
    )
    db_session.commit()
    gb = 1024**3
    body = {"name": "a.bin", "size": gb}
    assert http.post("/runner/v1/files/outputs/upload", headers=_auth(token), json=body).status_code == 200

    later = runner_routes._now() + timedelta(seconds=runner_api.URL_EXPIRES_SECONDS + 1)
    monkeypatch.setattr(runner_routes, "_now", lambda: later)
    body = {"name": "b.bin", "size": gb}
    assert http.post("/runner/v1/files/outputs/upload", headers=_auth(token), json=body).status_code == 200


def test_unfinished_uploads_are_aborted_only_once_their_job_has_ended(http, machine, store, db_session, member) -> None:  # noqa: F811
    from krater.compute import storage_cleanup
    from krater.services import compute_jobs

    job, row, token = machine
    size = runner_api.PART_BYTES + 1
    http.post("/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": "big.pt", "size": size})
    store.create_multipart_upload("projects/not-a-project/stray")

    assert storage_cleanup.abort_abandoned_uploads(db_session, store, now=NOW + timedelta(minutes=10)) == 1
    assert len(store.multipart_uploads) == 1  # the running job's upload is kept

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=10))
    assert storage_cleanup.abort_abandoned_uploads(db_session, store, now=NOW + timedelta(minutes=30)) == 0
    assert storage_cleanup.abort_abandoned_uploads(db_session, store, now=NOW + timedelta(minutes=71)) == 1
    assert store.multipart_uploads == {}


def test_a_machine_lists_only_the_inputs_its_job_asked_for(http, machine, store, db_session) -> None:
    job, _, token = machine
    job.input_names = ["data/", "vocab.txt"]
    db_session.flush()
    for name, size in (("data/a.csv", 4), ("data/sub/b.csv", 6), ("vocab.txt", 2), ("other.bin", 9)):
        store.put(f"projects/{job.project_id}/inputs/{name}", content_type="", size_bytes=size)

    started = http.post("/runner/v1/start", headers=_auth(token)).json()
    listed = http.post("/runner/v1/files/inputs/list", headers=_auth(token)).json()["files"]

    assert started["inputs"] is True
    assert sorted(f["name"] for f in listed) == ["data/a.csv", "data/sub/b.csv", "vocab.txt"]


def test_a_machine_cant_change_the_projects_inputs(http, machine, store) -> None:
    _, _, token = machine
    upload = http.post("/runner/v1/files/inputs/upload", headers=_auth(token), json={"name": "x", "size": 1})
    complete = http.post(
        "/runner/v1/files/inputs/complete", headers=_auth(token), json={"name": "x", "upload_id": "u", "etags": ["e"]}
    )
    assert upload.status_code == 403 and complete.status_code == 403
    assert store.list_prefix("projects/") == []


def test_a_box_deletes_from_its_workspace_and_nothing_else(http, machine, store, db_session) -> None:
    """The runner removes what the member deleted on the machine, so a restore doesn't bring it back."""
    job, _, token = machine
    job.kind = ComputeJobKind.BOX
    db_session.flush()
    prefix = f"projects/{job.project_id}/jobs/{job.id}/"
    for key in ("workspace/gone.txt", "workspace/dir/kept.txt", "checkpoints/c.pt", "outputs/o.json", "logs/l.log"):
        store.put(prefix + key, content_type="", size_bytes=1)
    other = "projects/other/jobs/other/workspace/gone.txt"
    store.put(other, content_type="", size_bytes=1)

    def delete(area: str, names, token: str | None = token):
        job.kind = ComputeJobKind.BOX  # a refusal rolls the test's session back
        db_session.flush()
        headers = _auth(token) if token else {}
        return http.post(f"/runner/v1/files/{area}/delete", headers=headers, json={"names": names})

    deleted = delete("workspace", ["gone.txt", "never-stored.txt"])
    refused = [delete(area, ["c.pt"]).status_code for area in ("checkpoints", "outputs", "logs", "inputs")]
    escaping = delete("workspace", ["../c.pt"])
    too_many = delete("workspace", ["x"] * 1001)
    no_token = delete("workspace", ["dir/kept.txt"], token=None)

    assert deleted.json() == {"deleted": 2}
    assert refused == [403, 403, 403, 403]
    assert escaping.status_code == 422 and too_many.status_code == 422 and no_token.status_code == 403
    left = sorted(item.key for item in store.list_prefix("projects/"))
    assert left == sorted(
        [other] + [prefix + k for k in ("workspace/dir/kept.txt", "checkpoints/c.pt", "outputs/o.json", "logs/l.log")]
    )


def test_a_job_cant_delete_stored_files(http, machine, store) -> None:
    job, _, token = machine
    key = f"projects/{job.project_id}/jobs/{job.id}/workspace/x.txt"
    store.put(key, content_type="", size_bytes=1)

    response = http.post("/runner/v1/files/workspace/delete", headers=_auth(token), json={"names": ["x.txt"]})

    assert response.status_code == 403
    assert [item.key for item in store.list_prefix("projects/")] == [key]


def test_inputs_deleted_after_queueing_fail_the_listing(http, machine, store, db_session) -> None:
    job, _, token = machine
    job.input_names = ["gone.tar"]
    db_session.flush()

    response = http.post("/runner/v1/files/inputs/list", headers=_auth(token))

    assert response.status_code == 422 and "gone.tar" in response.json()["error"] + str(response.json()["fields"])


def test_a_job_without_inputs_gets_none(http, machine, store) -> None:
    job, _, token = machine
    store.put(f"projects/{job.project_id}/inputs/a.csv", content_type="", size_bytes=4)
    assert http.post("/runner/v1/start", headers=_auth(token)).json()["inputs"] is False
    assert http.post("/runner/v1/files/inputs/list", headers=_auth(token)).json() == {"files": []}
