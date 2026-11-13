"""The `krater` command against a fake Krater and Weave (`httpx.MockTransport`)."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from krater_cli import auth, follow, main, store
from krater_cli.api import KraterError
from krater_cli.main import run
from krater_cli.resolve import ResolveError

KRATER = "https://krater.test"
WEAVE = "https://weave.test"
PROJECT = "11111111-1111-1111-1111-111111111111"
JOB = "22222222-2222-2222-2222-222222222222"
JOB_B = "22223333-3333-3333-3333-333333333333"  # shares the prefix "2222" with JOB
JOB_C = "9f9f9f9f-9999-9999-9999-999999999999"
PROJECT_B = "aaaa1111-1111-1111-1111-111111111111"
PROJECT_OLD = "bbbb2222-2222-2222-2222-222222222222"
STRANGER_JOB = "cccc3333-3333-3333-3333-333333333333"  # in a project the member isn't on


def job_row(job_id: str, name: str, *, state: str = "running", project: str = PROJECT) -> dict:
    return {
        "id": job_id,
        "project_id": project,
        "name": name,
        "kind": "job",
        "state": state,
        "state_reason": None,
        "target_usd_per_hour": "0.0700",
        "max_usd_per_hour": "0.1000",
        "spend_cents": 250,
        "exit_code": None,
    }


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.authorize_query: dict | None = None
        self.valid_access = {"access-1"}
        self.refreshes = 0
        self.jobs: list[dict] = []
        self.estimated: list[dict] = []
        self.secrets: dict[str, str] = {}
        # An older Krater ignores `smoke_minutes` and doesn't send it back.
        self.knows_smoke = True
        self.jobs_422 = False
        # Krater asks before queuing a job whose data transfer could cost a lot (409 with `confirm`).
        self.transfer_confirm = False
        self.projects = [
            {"id": PROJECT, "title": "Vision Lab", "status": "approved", "can_launch": True},
            {"id": PROJECT_B, "title": "Audio Lab", "status": "pending_review", "can_launch": False},
            {"id": PROJECT_OLD, "title": "Vision Old Notes", "status": "completed", "can_launch": False},
        ]
        self.project_jobs = {
            PROJECT: [job_row(JOB, "nb"), job_row(JOB_B, "train-b", state="queued")],
            PROJECT_B: [job_row(JOB_C, "sing", project=PROJECT_B)],
            PROJECT_OLD: [],
        }
        self.details: dict[str, dict] = {
            JOB: {
                "id": JOB,
                "project_id": PROJECT,
                "name": "nb",
                "kind": "box",
                "state": "running",
                "state_reason": None,
                "exit_code": None,
                "machine": {"ssh_host": "1.2.3.4", "ssh_port": 10483},
            },
            STRANGER_JOB: {
                "id": STRANGER_JOB,
                "project_id": PROJECT_OLD,
                "name": "theirs",
                "kind": "job",
                "state": "running",
                "state_reason": None,
                "exit_code": None,
            },
        }
        self.prices: list[dict] = []
        self.log_files: dict[int, bytes] = {}
        self.log_calls = 0
        self.before_log_call: dict[int, object] = {}
        self.files_listed: list[dict] | None = None
        self.files_after_expiry: list[dict] | None = None  # what the second listing says (fresh URLs)
        self.files_calls = 0
        self.expired_status = 403
        self.token_status: list[int] = []  # statuses the token endpoint answers a refresh with, before succeeding
        self.refresh_tokens = {"refresh-1"}
        self.revoked: list[dict] = []
        self.revoke_status = 200
        self.api_failures: list[object] = []  # raised or answered, one per API request, before it works
        self.inputs: dict[str, int] = {}  # the project's stored inputs, name -> size
        self.input_part_bytes = 64 * 1024 * 1024
        self.put_bodies: dict[str, bytes] = {}  # presigned PUTs, by URL path
        self.completed: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{KRATER}/api/v1/cli-config":
            return httpx.Response(
                200,
                json={
                    "issuer": WEAVE,
                    "client_id": "cli-app",
                    "scopes": "openid profile",
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                    "loopback": False,
                },
            )
        if url == f"{WEAVE}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={"authorization_endpoint": f"{WEAVE}/oauth/authorize", "token_endpoint": f"{WEAVE}/oauth/token"},
            )
        if url == f"{WEAVE}/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["the-code"] and form["client_id"] == ["cli-app"]
                assert form["code_verifier"][0] and form["redirect_uri"] == ["urn:ietf:wg:oauth:2.0:oob"]
                return httpx.Response(
                    200, json={"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 7200}
                )
            if self.token_status:
                return httpx.Response(self.token_status.pop(0), json={"error": "nope"})
            assert form["refresh_token"][0] in self.refresh_tokens
            self.refreshes += 1
            self.valid_access = {"access-2"}
            return httpx.Response(
                200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 7200}
            )
        if url == f"{WEAVE}/oauth/revoke":
            self.revoked.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(self.revoke_status)
        if url.startswith(f"{KRATER}/api/v1/") and self.api_failures:
            failure = self.api_failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, json={})
        if url.startswith(f"{KRATER}/api/v1/"):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token not in self.valid_access:
                return httpx.Response(401, json={"error": "expired"})
            return self.api(request, url.removeprefix(f"{KRATER}/api/v1"))
        if url.startswith("https://store.test/"):
            # A presigned URL: the CLI must not send its Krater sign-in along.
            assert "authorization" not in request.headers
            if request.method == "PUT":
                self.put_bodies[urlparse(url).path] = request.content
                return httpx.Response(200, headers={"ETag": f'"etag-{len(request.content)}"'})
            if "sig=old" in url:
                return httpx.Response(self.expired_status, text="Request has expired")
            return httpx.Response(
                200, content={"score": b"0.93\n", "log": b"hi\n", "ckpt": b"new!"}[urlparse(url).path.rsplit("/", 1)[1]]
            )
        return httpx.Response(404)

    def api(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/me":
            return httpx.Response(
                200, json={"name": "Ada", "email": "ada@example.org", "member": True, "reviewer": False, "admin": False}
            )
        if path == f"/projects/{PROJECT}/inputs" and request.method == "GET":
            return httpx.Response(
                200, json={"inputs": [{"name": n, "size_bytes": s} for n, s in sorted(self.inputs.items())]}
            )
        if path == f"/projects/{PROJECT}/inputs/upload":
            body = json.loads(request.content)
            name, size = body["name"], body["size"]
            if size <= self.input_part_bytes:
                self.inputs[name] = size
                return httpx.Response(200, json={"name": name, "url": f"https://store.test/put/{name}"})
            count = -(-size // self.input_part_bytes)
            parts = [f"https://store.test/part/{name}/{n}" for n in range(1, count + 1)]
            return httpx.Response(
                200, json={"name": name, "upload_id": "u1", "part_bytes": self.input_part_bytes, "parts": parts}
            )
        if path == f"/projects/{PROJECT}/inputs/complete":
            body = json.loads(request.content)
            self.completed.append(body)
            return httpx.Response(200, json={"ok": True})
        if path.startswith(f"/projects/{PROJECT}/inputs/") and request.method == "DELETE":
            name = path.removeprefix(f"/projects/{PROJECT}/inputs/")
            gone = [n for n in self.inputs if n == name or (name.endswith("/") and n.startswith(name))]
            for n in gone:
                del self.inputs[n]
            return httpx.Response(200, json={"deleted": len(gone)})
        if path == f"/projects/{PROJECT}/jobs/estimate" and request.method == "POST":
            self.estimated.append(json.loads(request.content))
            return httpx.Response(200, json={"up_to_cents": 400, "summary": "Up to $4.00 if it runs its full 8 hours."})
        if path == f"/projects/{PROJECT}/jobs" and request.method == "POST":
            body = json.loads(request.content)
            if self.transfer_confirm and body.get("confirm_transfer_cost") is not True:
                self.jobs.append({"refused": body})
                return httpx.Response(
                    409,
                    json={
                        "error": "This job's data transfer could cost up to $10.00.",
                        "confirm": "confirm_transfer_cost",
                    },
                )
            job = {
                "id": JOB,
                "name": body["name"],
                "kind": body["kind"],
                "state": "queued",
                "state_reason": "Queued.",
                "body": body,
            }
            if self.knows_smoke:
                job["smoke_minutes"] = body.get("smoke_minutes")
            self.jobs.append(job)
            return httpx.Response(201, json=job)
        acted = re.fullmatch(r"/jobs/([0-9a-f-]{36})/(cancel|stop|resume|move)", path)
        if acted and request.method == "POST":
            return httpx.Response(200, json=self.details[acted.group(1)])
        if re.fullmatch(r"/jobs/[0-9a-f-]{36}", path):
            detail = self.details.get(path.rsplit("/", 1)[1])
            if detail is None:
                return httpx.Response(404, json={"detail": f"No job {path.rsplit('/', 1)[1]}."})
            return httpx.Response(200, json=detail)
        if path.startswith(f"/jobs/{JOB}/logs") and self.log_files:
            return self.log_tail(path)
        if path == "/projects":
            return httpx.Response(200, json={"projects": self.projects})
        if path == "/prices":
            return httpx.Response(200, json={"prices": self.prices})
        if path.startswith(f"/jobs/{JOB}/logs"):
            return httpx.Response(
                200, json={"attempt": 2, "text": "epoch 9\ndone\n", "size_bytes": 99, "truncated": True}
            )
        if path == f"/jobs/{JOB}/files":
            self.files_calls += 1
            listed = self.files_after_expiry if self.files_calls > 1 and self.files_after_expiry else self.files_listed
            return httpx.Response(
                200,
                json={
                    "files": listed
                    or [
                        {
                            "area": "outputs",
                            "name": "eval/score.txt",
                            "size_bytes": 5,
                            "url": "https://store.test/score",
                        },
                        {"area": "logs", "name": "attempt-1.log", "size_bytes": 3, "url": "https://store.test/log"},
                        {"area": "outputs", "name": "../../escape.txt", "size_bytes": 1, "url": "https://store.test/x"},
                    ]
                },
            )
        if path == f"/projects/{PROJECT}/secrets" and request.method == "GET":
            return httpx.Response(
                200, json={"secrets": [{"name": n, "updated_at": "2026-10-11T12:00:00+00:00"} for n in self.secrets]}
            )
        if path.startswith(f"/projects/{PROJECT}/secrets/") and request.method == "PUT":
            name = path.rsplit("/", 1)[1]
            self.secrets[name] = json.loads(request.content)["value"]
            return httpx.Response(200, json={"name": name, "updated_at": "2026-10-11T12:00:00+00:00"})
        jobs_of = re.fullmatch(r"/projects/([0-9a-f-]{36})/jobs", path)
        if jobs_of and request.method == "GET":
            if self.jobs_422:
                return httpx.Response(422, json={"error": "Check the job's fields.", "fields": {"target": "too high"}})
            return httpx.Response(200, json={"jobs": self.project_jobs.get(jobs_of[1], [])})
        return httpx.Response(404, json={"error": "No such thing."})

    def log_tail(self, path: str) -> httpx.Response:
        """The server's `log_tail`: the end of the log, minus the partial first line unless the window is all of it."""
        self.log_calls += 1
        hook = self.before_log_call.pop(self.log_calls, None)
        if hook:
            hook()
        query = parse_qs(urlparse(path).query)
        count = int(query["bytes"][0])
        attempt = int(query["attempt"][0]) if "attempt" in query else max(self.log_files)
        raw = self.log_files[attempt]
        window = raw[-count:]
        truncated = len(raw) > len(window)
        text = window.decode(errors="replace")
        if truncated and "\n" in text:
            text = text.split("\n", 1)[1]
        return httpx.Response(
            200, json={"attempt": attempt, "text": text, "size_bytes": len(raw), "truncated": truncated}
        )


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KRATER_CONFIG_DIR", str(tmp_path / "krater"))
    return tmp_path / "krater"


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def http(fake: Fake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def _login(http: httpx.Client, said: list[str]) -> None:
    code = run(["login", "--url", KRATER, "--no-browser"], http=http, say=said.append, prompt=lambda _: " the-code \n")
    assert code == 0


def test_login_uses_pkce_and_stores_the_tokens_for_the_owner_only(http, fake, config_dir) -> None:
    said: list[str] = []
    _login(http, said)

    url = next(line for line in said if "oauth/authorize" in line).strip().split()[-1]
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["cli-app"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["urn:ietf:wg:oauth:2.0:oob"]
    assert said[-1] == f"Signed in to {KRATER} as Ada."
    creds = store.load()
    assert creds["access_token"] == "access-1" and creds["refresh_token"] == "refresh-1"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(store.credentials_path()).st_mode) == 0o600


def test_an_expired_access_token_is_refreshed_once(http, fake) -> None:
    _login(http, [])
    fake.valid_access = {"access-2"}  # Weave expired access-1 early

    said: list[str] = []
    assert run(["whoami"], http=http, say=said.append) == 0

    assert fake.refreshes == 1 and "Ada" in said[0]
    assert store.load()["refresh_token"] == "refresh-2"


def test_submit_sends_the_job_as_krater_expects(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    run(
        [
            "submit",
            PROJECT,
            "--name",
            "train",
            "--image",
            "ghcr.io/me/train:v3",
            "--command",
            "python train.py",
            "--gpu",
            "RTX 4090",
            "--target",
            "0.30",
            "--max",
            "0.45",
            "--max-runtime",
            "240",
        ],
        http=http,
        say=said.append,
    )

    body = fake.jobs[0]["body"]
    assert body["gpu_names"] == ["RTX 4090"] and body["target_usd_per_hour"] == "0.30"
    assert body["kind"] == "job" and body["interruptible"] is True and body["max_runtime_minutes"] == 240
    assert said == [f"Queued job {JOB}: Queued."]


def test_ssh_runs_ssh_with_the_boxs_address(http, fake) -> None:
    _login(http, [])
    commands: list[list[str]] = []

    run(["ssh", JOB, "--", "-L", "8888:localhost:8888"], http=http, exec_ssh=commands.append)

    assert commands == [["ssh", "-p", "10483", "-L", "8888:localhost:8888", "root@1.2.3.4"]]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ([], ["root@1.2.3.4"]),
        (["--", "-i", "key", "nvidia-smi", "-L"], ["-i", "key", "root@1.2.3.4", "nvidia-smi", "-L"]),
        (
            ["--", "-ikey", "-oBatchMode=yes", "-vA", "ls /workspace"],
            ["-ikey", "-oBatchMode=yes", "-vA", "root@1.2.3.4", "ls /workspace"],
        ),
        (["--", "-i", "key", "--", "-weird-command"], ["-i", "key", "root@1.2.3.4", "-weird-command"]),
        (["--", "uptime"], ["root@1.2.3.4", "uptime"]),
    ],
)
def test_ssh_puts_a_remote_command_after_the_address(extra, expected) -> None:
    # Staging 2026-10-14: `krater ssh box -- -i key 'hostname'` put the command before the address, and ssh took it
    # for the host name ("hostname contains invalid characters").
    assert main.ssh_command("1.2.3.4", 10483, extra) == ["ssh", "-p", "10483", *expected]


def test_a_secret_value_never_goes_on_the_command_line(http, fake) -> None:
    _login(http, [])

    run(["secrets", "set", PROJECT, "HF_TOKEN"], http=http, say=lambda _: None, read_secret=lambda name: "hf_abc")

    assert fake.secrets == {"HF_TOKEN": "hf_abc"}


def test_krater_errors_carry_the_field_problems(http, fake) -> None:
    _login(http, [])
    fake.jobs_422 = True
    with pytest.raises(KraterError) as error:
        run(["jobs", PROJECT], http=http)

    assert error.value.status == 422 and error.value.fields == {"target": "too high"}


def test_without_a_sign_in_the_cli_says_how_to(http) -> None:
    from krater_cli.auth import AuthError

    with pytest.raises(AuthError, match="krater login"):
        run(["projects"], http=http)


def test_logs_prints_the_end_of_the_output(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["logs", JOB, "--attempt", "2"], http=http, say=said.append) == 0

    assert said[0].startswith("[the last part of attempt 2's output")
    assert said[1] == "epoch 9\ndone"
    assert any("attempt=2" in str(r.url) for r in fake.requests)


def test_download_saves_each_file_under_its_kind_and_skips_odd_paths(http, fake, tmp_path) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["download", JOB, "--to", str(tmp_path / "results")], http=http, say=said.append) == 0

    assert (tmp_path / "results" / "outputs" / "eval" / "score.txt").read_bytes() == b"0.93\n"
    assert (tmp_path / "results" / "logs" / "attempt-1.log").read_bytes() == b"hi\n"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "results" / "escape.txt").exists()
    assert any(line.startswith("Skipped ../../escape.txt") for line in said)


# --- short ids and names ---------------------------------------------------------------------------------------------


def _paths(fake: Fake) -> list[str]:
    return [r.url.path.removeprefix("/api/v1") for r in fake.requests if r.url.path.startswith("/api/v1")]


def _show_ready(fake: Fake, job_id: str) -> None:
    fake.details[job_id].update(image="img", target_usd_per_hour=None, max_usd_per_hour=None, spend_cents=0)
    if fake.details[job_id].get("machine"):
        fake.details[job_id]["machine"].update(gpu="RTX 3060", usd_per_hour="0.0800", state="running")


def test_a_job_is_found_by_a_prefix_of_its_id(http, fake) -> None:
    _login(http, [])
    _show_ready(fake, JOB)
    said: list[str] = []

    assert run(["show", "22222"], http=http, say=said.append) == 0

    assert said[0] == "nb (box): running"
    assert f"/jobs/{JOB}" in _paths(fake)


def test_an_ambiguous_job_prefix_lists_the_candidates(http, fake) -> None:
    _login(http, [])

    with pytest.raises(ResolveError) as error:
        run(["show", "2222"], http=http)

    message = str(error.value)
    assert "'2222' matches more than one job" in message
    assert "22222222  nb (Vision Lab, running)" in message and "22223333  train-b (Vision Lab, queued)" in message


def test_a_job_is_found_by_its_name(http, fake) -> None:
    _login(http, [])
    fake.details[JOB_B] = {**fake.details[JOB], "id": JOB_B, "name": "train-b", "machine": None}
    _show_ready(fake, JOB_B)
    said: list[str] = []

    assert run(["show", "Train-B"], http=http, say=said.append) == 0

    assert f"/jobs/{JOB_B}" in _paths(fake)


def test_a_name_means_the_active_job_of_that_name(http, fake) -> None:
    """A job run again keeps its name: the finished runs don't make the name ambiguous."""
    _login(http, [])
    fake.project_jobs[PROJECT_OLD] = [job_row("dddd4444-4444-4444-4444-444444444444", "train-b", state="succeeded")]
    fake.details[JOB_B] = {**fake.details[JOB], "id": JOB_B, "name": "train-b", "machine": None}
    _show_ready(fake, JOB_B)

    assert run(["show", "train-b"], http=http, say=lambda _: None) == 0
    assert f"/jobs/{JOB_B}" in _paths(fake)

    fake.project_jobs[PROJECT][1]["state"] = "cancelled"  # now both have ended: say which
    with pytest.raises(ResolveError, match="'train-b' matches more than one job"):
        run(["show", "train-b"], http=http)


def test_an_unknown_job_prefix_says_so(http, fake) -> None:
    _login(http, [])

    with pytest.raises(ResolveError, match="No job matching 'deadbeef'."):
        run(["show", "deadbeef"], http=http)
    with pytest.raises(ResolveError, match="No job matching 'nope'."):
        run(["show", "nope"], http=http)
    with pytest.raises(ResolveError, match="at least 4"):
        run(["show", "22"], http=http)


def test_a_full_job_id_goes_straight_to_the_api_without_listing(http, fake) -> None:
    """An admin acts on jobs of projects they aren't on, which listing /projects would never show."""
    _login(http, [])
    _show_ready(fake, STRANGER_JOB)
    fake.requests.clear()

    assert run(["show", STRANGER_JOB], http=http, say=lambda _: None) == 0

    assert _paths(fake) == [f"/jobs/{STRANGER_JOB}"]


def test_a_project_is_found_by_id_prefix_or_by_part_of_its_title(http, fake) -> None:
    _login(http, [])

    for text in ("11111111", "1111", "vision lab", "VISION L", "ision la"):
        fake.requests.clear()
        run(["jobs", text], http=http, say=lambda _: None)
        assert f"/projects/{PROJECT}/jobs" in _paths(fake), text


def test_an_ambiguous_project_title_lists_the_candidates_and_an_exact_title_wins(http, fake) -> None:
    _login(http, [])

    with pytest.raises(ResolveError) as error:
        run(["jobs", "vision"], http=http)
    assert "aaaa" not in str(error.value)
    assert "11111111  Vision Lab" in str(error.value) and "bbbb2222  Vision Old Notes" in str(error.value)

    with pytest.raises(ResolveError, match="No project matching 'physics'."):
        run(["jobs", "physics"], http=http)

    fake.projects[2]["title"] = "Vision"  # "vision" is now one project's whole title
    fake.requests.clear()
    run(["jobs", "vision"], http=http, say=lambda _: None)
    assert f"/projects/{PROJECT_OLD}/jobs" in _paths(fake)


def test_a_full_project_id_is_not_looked_up(http, fake) -> None:
    _login(http, [])
    fake.requests.clear()

    run(["jobs", PROJECT.upper()], http=http, say=lambda _: None)

    assert _paths(fake) == [f"/projects/{PROJECT}/jobs"]


def test_the_tables_show_short_ids_and_json_keeps_the_full_ones(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    run(["jobs", "vision lab"], http=http, say=said.append)
    assert said[1].startswith("22222222  job") and JOB not in said[1]
    said.clear()
    run(["--json", "jobs", "vision lab"], http=http, say=said.append)
    assert JOB in said[0]


# --- projects --------------------------------------------------------------------------------------------------------


def test_projects_hides_finished_ones_unless_asked(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    run(["projects"], http=http, say=said.append)

    assert [line.split()[0] for line in said[:3]] == ["ID", "11111111", "aaaa1111"]
    assert said[-1] == "(1 finished project hidden; --all shows them)" and "Vision Old Notes" not in "".join(said)
    said.clear()
    run(["projects", "--all"], http=http, say=said.append)
    assert any("Vision Old Notes" in line for line in said) and not any("hidden" in line for line in said)


def test_projects_says_when_only_finished_ones_are_left(http, fake) -> None:
    _login(http, [])
    fake.projects = [fake.projects[2], {**fake.projects[2], "id": PROJECT, "status": "withdrawn"}]
    said: list[str] = []

    run(["projects"], http=http, say=said.append)

    assert said == ["(2 finished projects hidden; --all shows them)"]


# --- ssh -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["starting", "queued", "stopped", "paused", "failed"])
def test_ssh_only_connects_to_a_running_box(http, fake, state) -> None:
    _login(http, [])
    fake.details[JOB]["state"] = state  # the machine's address stays in the answer, as it did in the live test
    commands: list[list[str]] = []
    said: list[str] = []

    assert run(["ssh", JOB], http=http, say=said.append, exec_ssh=commands.append) == 1

    assert commands == []
    yet = " yet" if state in ("starting", "queued") else ""
    assert said == [f"nb isn't running{yet} ({state}). Try `krater show {JOB}`."]


def test_ssh_refuses_a_job_that_is_not_a_box(http, fake) -> None:
    _login(http, [])
    fake.details[JOB]["kind"] = "job"
    said: list[str] = []

    assert run(["ssh", JOB], http=http, say=said.append, exec_ssh=lambda c: None) == 1
    assert "not a box" in said[0]


def test_ssh_on_windows_runs_ssh_and_returns_its_exit_code(http, fake, monkeypatch) -> None:
    _login(http, [])
    calls: list[list[str]] = []
    monkeypatch.setattr(main, "_is_windows", lambda: True)
    monkeypatch.setattr(main.shutil, "which", lambda name: "ssh.exe")
    monkeypatch.setattr(main.subprocess, "call", lambda command: calls.append(command) or 255)
    monkeypatch.setattr(main.os, "execvp", lambda *a: pytest.fail("execvp detaches from a Windows console"))

    assert run(["ssh", JOB], http=http, say=lambda _: None) == 255

    assert calls == [["ssh", "-p", "10483", "root@1.2.3.4"]]


def test_ssh_elsewhere_replaces_the_process(http, fake, monkeypatch) -> None:
    _login(http, [])
    calls: list[tuple] = []
    monkeypatch.setattr(main, "_is_windows", lambda: False)
    monkeypatch.setattr(main.shutil, "which", lambda name: "/usr/bin/ssh")
    monkeypatch.setattr(main.os, "execvp", lambda *a: calls.append(a))

    run(["ssh", JOB], http=http, say=lambda _: None)

    assert calls == [("ssh", ["ssh", "-p", "10483", "root@1.2.3.4"])]


# --- reading a secret ------------------------------------------------------------------------------------------------


class _Stdin:
    def __init__(self, *, tty: bool, text: str = "", fileno: int = 0) -> None:
        self._tty, self._text, self._fileno = tty, text, fileno

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        return self._fileno

    def read(self) -> str:
        return self._text


def test_a_pipe_or_redirect_is_not_a_terminal(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    assert main._stdin_is_terminal() is False


def test_on_windows_a_tty_that_is_not_a_console_is_not_a_terminal(monkeypatch) -> None:
    """NUL answers isatty() with True on Windows, so `secrets set ... < NUL` waited at a prompt forever."""
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True, fileno=7))
    monkeypatch.setattr(main, "_is_windows", lambda: True)
    asked: list[int] = []
    monkeypatch.setattr(main, "_windows_console", lambda fileno: asked.append(fileno) or False)

    assert main._stdin_is_terminal() is False
    assert asked == [7]

    monkeypatch.setattr(main, "_windows_console", lambda fileno: True)
    assert main._stdin_is_terminal() is True

    def broken(fileno):
        raise OSError("bad handle")

    monkeypatch.setattr(main, "_windows_console", broken)
    assert main._stdin_is_terminal() is False


def test_elsewhere_isatty_is_enough(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(main, "_is_windows", lambda: False)
    assert main._stdin_is_terminal() is True


@pytest.mark.skipif(os.name != "nt", reason="the Windows console API")
def test_the_real_nul_device_is_not_a_windows_console() -> None:
    with open(os.devnull) as nul:
        assert nul.isatty()  # the trap: Python says yes
        assert main._windows_console(nul.fileno()) is False


def test_read_secret_reads_stdin_when_nobody_is_typing(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False, text="s3cret\n"))
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt: pytest.fail("must not prompt"))

    assert main._read_secret("HF_TOKEN") == "s3cret"


# --- logs --follow and wait ------------------------------------------------------------------------------------------


def _follow(http, fake, steps, *extra) -> tuple[int, list[str]]:
    """Run `logs -f`; each sleep runs the next step, which changes the fake's log or job."""
    said: list[str] = []
    pending = list(steps)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        pending.pop(0)()

    code = run(["logs", JOB, "-f", *extra], http=http, say=said.append, sleep=sleep)
    assert not pending, "the job finished before the last step"
    said.append(f"sleeps={sleeps}")
    return code, said


def _append(fake: Fake, attempt: int, data: bytes):
    return lambda: fake.log_files.__setitem__(attempt, fake.log_files.get(attempt, b"") + data)


def test_logs_follow_prints_only_what_is_new_until_the_job_finishes(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"a\nb\n"}

    def finish() -> None:
        _append(fake, 1, b"e\n")()
        fake.details[JOB].update(state="succeeded")

    code, said = _follow(http, fake, [_append(fake, 1, b"c\nd\n"), finish])

    assert code == 0
    assert said == ["a\nb", "c\nd", "e", "nb: succeeded", "sleeps=[5, 5]"]


def test_logs_follow_uses_the_interval_and_does_not_refetch_an_unchanged_log(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"a\n"}
    fake.details[JOB].update(state="failed")

    _, said = _follow(http, fake, [], "--interval", "2")
    assert said == ["a", "nb: failed", "sleeps=[]"]

    fake.details[JOB].update(state="running")
    fake.log_calls = 0
    _, said = _follow(http, fake, [lambda: fake.details[JOB].update(state="failed")], "--interval", "2")
    assert said[-1] == "sleeps=[2.0]"
    assert fake.log_calls == 2  # the first tail and one 1-byte probe; no growth, so no third request


def test_logs_follow_starts_over_when_the_attempt_changes(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"one\ntwo\n"}

    def finish() -> None:
        _append(fake, 2, b"y\n")()
        fake.details[JOB].update(state="succeeded")

    # attempt 2's log is shorter than attempt 1's: only the attempt number says it started over
    _, said = _follow(http, fake, [_append(fake, 2, b"x\n"), finish])

    assert said[:5] == ["one\ntwo", "--- attempt 2 ---", "x", "y", "nb: succeeded"]


def test_logs_follow_does_not_lose_bytes_that_arrive_between_its_two_requests(http, fake, monkeypatch) -> None:
    monkeypatch.setattr(follow, "FIRST_OVERLAP", 8)
    _login(http, [])
    fake.log_files = {1: b"aaaa\nbbbb\n"}

    def grow() -> None:
        _append(fake, 1, b"cccc\n")()
        # call 1 was the first tail and call 2 the probe; call 3 (the new bytes) finds more than the probe saw
        fake.before_log_call[3] = _append(fake, 1, b"dddd\n")

    _, said = _follow(http, fake, [grow, lambda: fake.details[JOB].update(state="succeeded")])

    assert said[:3] == ["aaaa\nbbbb", "cccc\ndddd", "nb: succeeded"]


def test_logs_follow_widens_its_window_when_the_apis_dropped_line_eats_new_bytes(http, fake, monkeypatch) -> None:
    """A progress bar leaves the old log ending in a long partial line; the server drops that line from a window."""
    monkeypatch.setattr(follow, "FIRST_OVERLAP", 4)
    _login(http, [])
    fake.log_files = {1: b"x" * 50}

    def finish() -> None:
        _append(fake, 1, b"yyy\nzzz\n")()
        fake.details[JOB].update(state="succeeded")

    _, said = _follow(http, fake, [finish])

    assert said[:3] == ["x" * 50, "yyy\nzzz", "nb: succeeded"]
    assert fake.log_calls > 3  # first tail, probe, then the new bytes asked for again with a wider window


def test_logs_follow_says_when_it_skips_more_than_a_megabyte(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"start\n"}

    def burst() -> None:
        _append(fake, 1, (b"x" * 99 + b"\n") * 15000)()  # 1.5 MB at once
        fake.details[JOB].update(state="succeeded")

    _, said = _follow(http, fake, [burst])

    skipped = int(re.fullmatch(r"\[\.\.\. skipped (\d+) bytes \.\.\.\]", said[1])[1])
    assert said[0] == "start" and 400_000 < skipped < 500_000
    assert len(said[2]) < 1024 * 1024 and said[2].endswith("x" * 99)


def test_logs_follow_ctrl_c_exits_quietly_with_130(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"a\n"}
    said: list[str] = []

    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    assert run(["logs", JOB, "-f"], http=http, say=said.append, sleep=interrupted) == 130
    assert said == ["a"]


def test_logs_follow_is_for_the_latest_attempt_as_text(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["logs", JOB, "-f", "--attempt", "1"], http=http, say=said.append) == 2
    assert run(["--json", "logs", JOB, "-f"], http=http, say=said.append) == 2


@pytest.mark.parametrize(
    ("state", "exit_code", "expected"),
    [("succeeded", 0, 0), ("failed", 3, 3), ("failed", None, 1), ("failed", 0, 1), ("cancelled", None, 1)],
)
def test_wait_exits_with_the_jobs_outcome(http, fake, state, exit_code, expected) -> None:
    _login(http, [])
    said: list[str] = []
    steps = iter([lambda: fake.details[JOB].update(state=state, exit_code=exit_code, state_reason="because")])

    code = run(["wait", JOB], http=http, say=said.append, sleep=lambda s: next(steps)())

    assert code == expected
    assert said == ["nb: running", f"nb: {state} - because"]


def test_wait_gives_up_after_the_timeout_with_124(http, fake) -> None:
    _login(http, [])
    now = [1000.0]
    said: list[str] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds

    argv = ["wait", JOB, "--timeout", "1", "--interval", "20"]
    code = run(argv, http=http, say=said.append, sleep=sleep, clock=lambda: now[0])

    assert code == 124
    assert said == ["nb: running", "Gave up after 1 minute; nb is still running."]


def test_wait_with_json_prints_only_the_finished_job(http, fake) -> None:
    _login(http, [])
    fake.details[JOB].update(state="succeeded", exit_code=0)
    said: list[str] = []

    assert run(["--json", "wait", JOB], http=http, say=said.append) == 0

    assert json.loads(said[0])["state"] == "succeeded" and len(said) == 1


# --- prices ----------------------------------------------------------------------------------------------------------


def _prices(fake: Fake) -> None:
    def row(gpu, rental, cheapest, median, offers):
        return {
            "gpu": gpu,
            "rental": rental,
            "min_usd_per_hour": cheapest,
            "median_usd_per_hour": median,
            "offers": offers,
            "sampled_at": "2026-11-13T01:00:12Z",
        }

    fake.prices = [
        row("RTX 4090", "bid", "0.2000", "0.3000", 40),
        row("RTX 4090", "ondemand", "0.4000", "0.5000", 25),
        row("RTX 3060", "bid", "0.0700", "0.0900", 12),
        row("H100", "bid", None, None, 0),
        row("RTX 3060", "ondemand", "0.1000", "0.1200", 3),
    ]


def test_prices_lists_gpus_with_offers_cheapest_first_in_the_web_uis_words(http, fake) -> None:
    _login(http, [])
    _prices(fake)
    said: list[str] = []

    run(["prices"], http=http, say=said.append)

    assert said[0] == "At Krater's hourly price check of 01:00 UTC, per hour:"
    assert [line.split()[0:3] for line in said[2:]] == [
        ["RTX", "3060", "interruptible"],
        ["RTX", "3060", "on"],
        ["RTX", "4090", "interruptible"],
        ["RTX", "4090", "on"],
    ]
    assert "on demand" in said[3] and "H100" not in "".join(said) and "ondemand" not in "".join(said)


def test_prices_all_and_gpu_filters(http, fake) -> None:
    _login(http, [])
    _prices(fake)
    said: list[str] = []

    run(["prices", "--all"], http=http, say=said.append)
    assert len(said) == 7 and said[-1].startswith("H100") and said[-1].split()[-3:] == ["-", "-", "0"]
    said.clear()
    run(["prices", "--gpu", "4090"], http=http, say=said.append)
    assert len(said) == 4 and all("4090" in line for line in said[2:])
    said.clear()
    run(["prices", "--gpu", "h100"], http=http, say=said.append)
    assert said == ["Nobody is offering those right now (--all lists them anyway)."]
    said.clear()
    run(["prices", "--gpu", "a100"], http=http, say=said.append)
    assert said == ["No GPU model matches 'a100'."]


# --- submit ----------------------------------------------------------------------------------------------------------

SUBMIT = ["submit", PROJECT, "--name", "t", "--image", "img", "--gpu", "RTX 3060"]
GIVE_PRICES = (
    "Give a limit in USD per hour, e.g. --limit 0.07: it starts at or under it, never pays more, and keeps its "
    "machine when outbid. Or --target (start at or under) with --max (never above)."
)


def test_submit_with_a_limit_places_a_limit_order(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run([*SUBMIT, "--limit", "$0.07", "--expires", "12h"], http=http, say=said.append) == 0

    body = fake.jobs[0]["body"]
    assert body["limit_usd_per_hour"] == "0.07" and "target_usd_per_hour" not in body
    assert (body["max_wait_minutes"], body["on_max_wait"]) == (720, "cancel")
    assert "on_outbid" not in body  # Krater's default for a limit order: hold


@pytest.mark.parametrize(
    ("extra", "complaint"),
    [
        (["--limit", "0.07", "--target", "0.05", "--max", "0.08"], "not both"),
        (["--limit", "0.07", "--on-demand"], "can't be --on-demand"),
    ],
)
def test_a_limit_with_other_prices_is_refused_before_asking_krater(http, fake, extra, complaint) -> None:
    _login(http, [])
    said: list[str] = []

    assert run([*SUBMIT, *extra], http=http, say=said.append) == 2

    assert fake.jobs == [] and complaint in said[-1]


@pytest.mark.parametrize(("text", "minutes"), [("90m", 90), ("12h", 720), ("2d", 2880), ("45", 45), ("1.5h", 90)])
def test_durations(text, minutes) -> None:
    assert main._duration(text) == minutes


def test_a_limit_order_shows_as_one_price(http, fake) -> None:
    assert main._job_price({"limit_order": True, "max_usd_per_hour": "0.0800"}) == "limit $0.0800"
    plain = {"target_usd_per_hour": "0.0700", "max_usd_per_hour": "0.1000"}
    assert main._job_price(plain) == "$0.0700/$0.1000"  # an older Krater sends no limit_order
    assert main._job_price({**plain, "on_outbid": "hold"}, long=True).endswith("keeps its machine when outbid")


def test_submit_without_prices_shows_the_market_and_explains(http, fake) -> None:
    _login(http, [])
    _prices(fake)
    said: list[str] = []

    assert run(SUBMIT, http=http, say=said.append) == 2

    assert fake.jobs == []
    assert said[0] == "At Krater's hourly price check of 01:00 UTC, per hour, interruptible:"
    assert said[2].split() == ["RTX", "3060", "$0.0700", "$0.0900", "12"] and len(said) == 4
    assert said[-1] == GIVE_PRICES
    said.clear()
    assert run([*SUBMIT, "--target", "0.07", "--on-demand"], http=http, say=said.append) == 2
    assert said[0] == "At Krater's hourly price check of 01:00 UTC, per hour, on demand:" and "$0.1000" in said[2]


def test_submit_without_prices_or_gpu_lists_every_gpu_with_offers(http, fake) -> None:
    _login(http, [])
    _prices(fake)
    said: list[str] = []

    run(["submit", PROJECT, "--name", "t", "--image", "img"], http=http, say=said.append)

    assert [line.split()[1] for line in said[2:-1]] == ["3060", "4090"] and "H100" not in "".join(said)
    said.clear()
    run([*SUBMIT[:-2], "--gpu", "A100"], http=http, say=said.append)
    assert said[0] == "No price data for 'a100'."


def test_submit_accepts_a_dollar_sign_on_prices(http, fake) -> None:
    _login(http, [])

    assert run([*SUBMIT, "--target", "$0.07", "--max", " $0.10"], http=http, say=lambda _: None) == 0

    body = fake.jobs[0]["body"]
    assert body["target_usd_per_hour"] == "0.07" and body["max_usd_per_hour"] == "0.10"


def test_submit_rejects_a_price_that_is_not_a_number(http, fake, capsys) -> None:
    with pytest.raises(SystemExit) as exit_:
        run([*SUBMIT, "--target", "cheap", "--max", "1"], http=http)

    assert exit_.value.code == 2 and "isn't a price" in capsys.readouterr().err


def test_submit_finds_the_project_by_title(http, fake) -> None:
    _login(http, [])

    assert run(["submit", "vision lab", *SUBMIT[2:], "--target", "1", "--max", "2"], http=http, say=lambda _: None) == 0
    assert fake.jobs


# --- errors ----------------------------------------------------------------------------------------------------------


def test_krater_errors_get_a_message_from_whichever_field_the_api_used() -> None:
    from krater_cli.api import _problem

    assert _problem(403, "/jobs/x/cancel", {"error": "Not yours."}) == ("Not yours.", {})
    assert _problem(404, "/jobs/abc", {"detail": "No job abc."}) == ("No job abc.", {})
    # FastAPI's own validation of a malformed id
    body = {"detail": [{"type": "uuid_parsing", "loc": ["path", "job_id"], "msg": "Input should be a valid UUID"}]}
    assert _problem(422, "/jobs/7b0ffa48", body) == (
        "Krater didn't accept that.",
        {"job_id": "Input should be a valid UUID"},
    )
    assert _problem(404, "/jobs/7b0ffa48", {}) == ("No job 7b0ffa48.", {})
    assert _problem(404, "/projects/p1/jobs", None) == ("No project p1.", {})
    assert _problem(502, "/me", {}) == ("Krater answered 502.", {})
    assert _problem(422, "/projects/p/jobs", {"error": "Check.", "fields": {"a": "b"}}) == ("Check.", {"a": "b"})


def test_a_404_without_an_error_key_still_says_what_is_missing(http, fake) -> None:
    _login(http, [])
    missing = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    with pytest.raises(KraterError, match=f"No job {missing}."):
        run(["show", missing], http=http)


def test_report_prints_the_message_and_one_line_per_field(capsys) -> None:
    assert main.report(KraterError("Check the job's fields.", status=422, fields={"target": "too high"})) == 1

    assert capsys.readouterr().err == "krater: Check the job's fields.\n  target: too high\n"


def test_report_says_which_host_could_not_be_reached(capsys) -> None:
    request = httpx.Request("GET", "https://weave.example.org/oauth/token")

    main.report(httpx.ConnectError("boom", request=request))
    main.report(httpx.ReadTimeout("slow", request=request))
    main.report(httpx.ConnectError("no request attached"))

    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("krater: couldn't reach weave.example.org (ConnectError)")
    assert lines[1] == "krater: weave.example.org didn't answer in time."
    assert lines[2].startswith("krater: couldn't reach Krater or Weave (ConnectError)")


# --- download ---------------------------------------------------------------------------------------------------------


def test_download_fetches_everything_again_unless_told_to_skip_same_size_files(http, fake, tmp_path) -> None:
    # The file list has no modified time or etag, and a re-uploaded checkpoint keeps its size.
    _login(http, [])
    results = tmp_path / "results"
    run(["download", JOB, "--to", str(results)], http=http, say=lambda _: None)
    (results / "logs" / "attempt-1.log").write_bytes(b"old")  # same size as the real one
    (results / "outputs" / "eval" / "score.txt").write_bytes(b"x")
    fake.requests.clear()
    said: list[str] = []

    run(["download", JOB, "--to", str(results)], http=http, say=said.append)

    assert (results / "logs" / "attempt-1.log").read_bytes() == b"hi\n"
    assert (results / "outputs" / "eval" / "score.txt").read_bytes() == b"0.93\n"
    assert sorted(r.url.path for r in fake.requests if r.url.host == "store.test") == ["/log", "/score"]
    assert said[-1] == f"Saved 2 files: 8 B in all, under {results}."


def test_download_skip_existing_keeps_files_of_the_same_size_and_adds_up_sizes(http, fake, tmp_path) -> None:
    _login(http, [])
    results = tmp_path / "results"
    run(["download", JOB, "--to", str(results)], http=http, say=lambda _: None)
    (results / "logs" / "attempt-1.log").write_bytes(b"old")  # same size as the real one: counts as here
    (results / "outputs" / "eval" / "score.txt").write_bytes(b"x")  # a different size: fetched again
    fake.requests.clear()
    said: list[str] = []

    run(["download", JOB, "--to", str(results), "--skip-existing"], http=http, say=said.append)

    assert "logs/attempt-1.log  already here" in said
    assert "outputs/eval/score.txt  (5 B)" in said
    assert (results / "outputs" / "eval" / "score.txt").read_bytes() == b"0.93\n"
    assert [r.url.path for r in fake.requests if r.url.host == "store.test"] == ["/score"]
    assert said[-1] == f"Saved 1 file, 1 already here: 8 B in all, under {results}."


def _two_files(*, old: bool) -> list[dict]:
    sig = "?sig=old" if old else "?sig=new"
    return [
        {"area": "outputs", "name": "a.txt", "size_bytes": 5, "url": "https://store.test/score" + sig},
        {"area": "checkpoints", "name": "step1.pt", "size_bytes": 4, "url": "https://store.test/ckpt" + sig},
    ]


@pytest.mark.parametrize("status", [403, 400])
def test_download_asks_for_fresh_urls_when_a_presigned_one_has_expired(http, fake, tmp_path, status) -> None:
    _login(http, [])
    fake.expired_status = status
    fake.files_listed = _two_files(old=True)
    fake.files_after_expiry = _two_files(old=False)
    said: list[str] = []

    code = run(["download", JOB, str(tmp_path / "r")], http=http, say=said.append)

    assert code == 0
    assert (tmp_path / "r" / "outputs" / "a.txt").read_bytes() == b"0.93\n"
    assert (tmp_path / "r" / "checkpoints" / "step1.pt").read_bytes() == b"new!"
    assert fake.files_calls == 2  # once more, not once per file: the second file got its fresh URL from that list
    assert not list((tmp_path / "r").rglob("*.partial"))
    assert said[-1].startswith("Saved 2 files: 9 B")


def test_download_gives_up_on_a_file_whose_fresh_url_is_refused_too(http, fake, tmp_path) -> None:
    _login(http, [])
    fake.files_listed = _two_files(old=True)
    fake.files_after_expiry = _two_files(old=True)

    with pytest.raises(httpx.HTTPStatusError):
        run(["download", JOB, str(tmp_path / "r")], http=http, say=lambda _: None)

    assert fake.files_calls == 2


@pytest.mark.parametrize("name", [".", "a/./b", "a//b", "a/../b", ""])
def test_download_skips_names_with_dot_or_empty_segments(http, fake, tmp_path, name) -> None:
    _login(http, [])
    fake.files_listed = [
        {"area": "outputs", "name": name, "size_bytes": 5, "url": "https://store.test/score"},
        {"area": "outputs", "name": "ok.txt", "size_bytes": 5, "url": "https://store.test/score"},
    ]
    said: list[str] = []

    assert run(["download", JOB, str(tmp_path / "r")], http=http, say=said.append) == 0

    assert f"Skipped {name}: not a plain relative path." in said
    assert sorted(p.name for p in (tmp_path / "r" / "outputs").iterdir()) == ["ok.txt"]


def test_download_reports_a_file_it_cannot_write_and_carries_on(http, fake, tmp_path) -> None:
    _login(http, [])
    results = tmp_path / "r"
    (results / "outputs").mkdir(parents=True)
    (results / "outputs" / "a.txt").mkdir()  # a directory where the file should go
    fake.files_listed = [
        {"area": "outputs", "name": "a.txt", "size_bytes": 5, "url": "https://store.test/score"},
        {"area": "outputs", "name": "b.txt", "size_bytes": 5, "url": "https://store.test/score"},
    ]
    said: list[str] = []

    assert run(["download", JOB, str(results)], http=http, say=said.append) == 1

    assert any(line.startswith("outputs/a.txt  failed:") for line in said)
    assert (results / "outputs" / "b.txt").read_bytes() == b"0.93\n"
    assert said[-1] == f"Saved 1 file, 1 failed: 10 B in all, under {results}."


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_sizes_are_human_readable(count, text) -> None:
    assert main._size(count) == text


# --- open ------------------------------------------------------------------------------------------------------------


def test_open_a_job_by_id_prefix_opens_its_page(http, fake) -> None:
    _login(http, [])
    opened: list[str] = []
    said: list[str] = []

    assert run(["open", "9f9f9f"], http=http, say=said.append, open_url=opened.append) == 0

    page = f"{KRATER}/projects/{PROJECT_B}/compute/{JOB_C}"
    assert opened == [page] and said == [page]


def test_open_a_project_by_title_opens_its_page(http, fake) -> None:
    _login(http, [])
    opened: list[str] = []

    run(["open", "audio"], http=http, say=lambda _: None, open_url=opened.append)

    assert opened == [f"{KRATER}/projects/{PROJECT_B}"]


def test_open_with_a_full_id_tries_a_job_and_then_a_project(http, fake) -> None:
    _login(http, [])
    opened: list[str] = []

    run(["open", STRANGER_JOB], http=http, say=lambda _: None, open_url=opened.append)
    run(["open", PROJECT], http=http, say=lambda _: None, open_url=opened.append)

    assert opened == [f"{KRATER}/projects/{PROJECT_OLD}/compute/{STRANGER_JOB}", f"{KRATER}/projects/{PROJECT}"]


def test_open_an_ambiguous_or_unknown_target_opens_nothing(http, fake) -> None:
    _login(http, [])
    opened: list[str] = []

    with pytest.raises(ResolveError, match="more than one job or project"):
        run(["open", "2222"], http=http, open_url=opened.append)
    with pytest.raises(ResolveError, match="No job or project matching 'zzz'."):
        run(["open", "zzz"], http=http, open_url=opened.append)
    assert opened == []


def test_download_still_takes_to_for_the_folder(http, fake, tmp_path) -> None:
    _login(http, [])

    assert run(["download", JOB, "--to", str(tmp_path / "r")], http=http, say=lambda _: None) == 0

    assert (tmp_path / "r" / "outputs" / "eval" / "score.txt").exists()


def test_stop_says_the_box_is_stopping(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["stop", "nb"], http=http, say=said.append) == 0

    assert said == [
        "nb: stopping. Its /workspace is saved first, then the machine stops (`krater show 22222222` says when)."
    ]


@pytest.mark.parametrize(("state", "shown"), [("running", True), ("stopped", False), ("starting", False)])
def test_show_gives_the_ssh_command_only_while_the_box_runs(http, fake, state, shown) -> None:
    _login(http, [])
    _show_ready(fake, JOB)
    fake.details[JOB]["state"] = state
    said: list[str] = []

    run(["show", JOB], http=http, say=said.append)

    assert any(line.startswith("  ssh: krater ssh 22222222  (ssh -p 10483 root@1.2.3.4)") for line in said) is shown


# --- review fixes: transient errors, prices, refresh, terminal output, logout ---------------------------------------------


def _wait(http, *extra) -> tuple[int, list[str], list[float]]:
    said: list[str] = []
    sleeps: list[float] = []
    code = run(["wait", JOB, *extra], http=http, say=said.append, sleep=sleeps.append)
    return code, said, sleeps


def test_wait_rides_out_a_5xx_a_429_and_a_dropped_connection(http, fake, capsys) -> None:
    _login(http, [])
    fake.details[JOB].update(state="succeeded", exit_code=0)
    fake.api_failures = [503, httpx.ConnectError("refused"), 429, httpx.ReadTimeout("slow")]

    code, said, sleeps = _wait(http)

    assert code == 0 and said == ["nb: succeeded"]
    # The 503 and the refused connection are tried again at once by the API client (1 s, then 2 s, as for a
    # deploy); the 429 and the timeout by `wait`, with a growing pause starting at its interval.
    assert sleeps == [1.0, 2.0, 5, 10]
    assert capsys.readouterr().err.count("trying again") == 2


def test_wait_backs_off_no_further_than_a_minute(http, fake) -> None:
    _login(http, [])
    fake.details[JOB].update(state="succeeded", exit_code=0)
    fake.api_failures = [500] * 8

    _, _, sleeps = _wait(http)

    assert max(sleeps) == 60 and sleeps[-1] == 60


def test_wait_still_stops_at_a_4xx_other_than_429(http, fake) -> None:
    _login(http, [])
    fake.api_failures = [403]

    with pytest.raises(KraterError) as caught:
        _wait(http)

    assert caught.value.status == 403


def test_wait_times_out_with_124_while_krater_is_down(http, fake) -> None:
    _login(http, [])
    fake.api_failures = [503] * 100
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    said: list[str] = []
    code = run(["wait", JOB, "--timeout", "1"], http=http, say=said.append, sleep=sleep, clock=lambda: now[0])

    assert code == 124 and said[0].startswith("Gave up after 1 minute;")


def test_logs_follow_rides_out_a_5xx_too(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"a\n"}
    fake.details[JOB].update(state="succeeded")
    fake.api_failures = [502, httpx.ConnectError("refused")]

    code, said = _follow(http, fake, [lambda: None, lambda: None])

    # The API client rides both out itself (a deploy's moment of 502s), before `logs -f` has to.
    assert code == 0 and said == ["a", "nb: succeeded", "sleeps=[1.0, 2.0]"]


@pytest.mark.parametrize("price", ["nan", "NaN", "inf", "$snan"])
def test_submit_rejects_a_price_that_is_not_finite(http, fake, capsys, price) -> None:
    with pytest.raises(SystemExit) as exit_:
        run([*SUBMIT, "--limit", price], http=http)

    assert exit_.value.code == 2 and "isn't a price" in capsys.readouterr().err


def test_a_refresh_token_another_krater_rotated_is_picked_up_from_disk(http, fake) -> None:
    _login(http, [])
    krater = main.Krater(http)  # a long-lived process: it loaded refresh-1
    store.save({**store.load(), "refresh_token": "refresh-9"})  # another invocation rotated it meanwhile
    fake.valid_access = {"access-2"}
    fake.token_status = [400]  # Weave refuses the spent refresh-1
    fake.refresh_tokens = {"refresh-9"}

    assert krater.get("/me")["name"] == "Ada"

    assert fake.refreshes == 1 and store.load()["refresh_token"] == "refresh-2"


def test_a_refused_refresh_with_nothing_new_on_disk_says_to_sign_in_again(http, fake) -> None:
    _login(http, [])
    fake.valid_access = {"access-2"}
    fake.token_status = [400]  # a second try (wrongly) would succeed

    with pytest.raises(auth.AuthError, match="sign-in has expired"):
        run(["whoami"], http=http, say=lambda _: None)


@pytest.mark.parametrize("status", [500, 503, 429])
def test_a_weave_outage_during_refresh_is_not_reported_as_an_expired_sign_in(http, fake, status) -> None:
    _login(http, [])
    fake.valid_access = {"access-2"}
    fake.token_status = [status]

    with pytest.raises(auth.AuthError, match="Weave is unavailable") as caught:
        run(["whoami"], http=http, say=lambda _: None)

    assert "expired" not in str(caught.value) and store.load()["refresh_token"] == "refresh-1"


def test_saves_use_their_own_temp_file_and_leave_nothing_behind(config_dir, monkeypatch) -> None:
    sources: list[str] = []
    real_replace = os.replace

    def replace(src, dst):
        sources.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", replace)

    store.save({"n": 1})
    store.save({"n": 2})

    assert len(set(sources)) == 2 and "credentials.tmp" not in sources  # no shared temp name to interleave in
    assert store.load() == {"n": 2}
    assert [p.name for p in config_dir.iterdir()] == ["credentials.json"]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(store.credentials_path()).st_mode) == 0o600


def test_a_failed_save_removes_its_temp_file(config_dir, monkeypatch) -> None:
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        store.save({"a": 1})

    assert list(config_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("plain\ttab\nline", "plain\ttab\nline"),
        ("a\x1b[2Jb\x1b[31mred\x1b[0m", "abred"),
        ("t\x1b]0;owned\x07itle", "title"),
        ("t\x1b]0;owned\x1b\\itle", "title"),
        ("bell\x07 nul\x00 del\x7f c1\x9b[2J", "bell nul del c1[2J"),
        ("progress 10%\rprogress 20%", "progress 10%\rprogress 20%"),
        ("lone \x1b", "lone "),
    ],
)
def test_clean_strips_terminal_control_sequences(raw, shown) -> None:
    assert follow.clean(raw) == shown


EVIL = b"ok\x1b[2J\x1b]0;pwned\x07 done\n"


def test_logs_strips_control_characters_unless_raw(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: EVIL}
    said: list[str] = []

    run(["logs", JOB], http=http, say=said.append)
    run(["logs", JOB, "--raw"], http=http, say=said.append)

    assert said == ["ok done", EVIL.decode().rstrip("\n")]


def test_logs_follow_strips_control_characters_unless_raw(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: EVIL}
    fake.details[JOB].update(state="succeeded")

    _, cleaned = _follow(http, fake, [])
    _, raw = _follow(http, fake, [], "--raw")

    assert cleaned[0] == "ok done" and raw[0] == EVIL.decode().rstrip("\n")


def test_a_job_name_is_cleaned_in_the_state_line() -> None:
    assert follow.state_line({"name": "n\x1b[2Jb", "state": "failed", "state_reason": "x\x07y"}) == "nb: failed - xy"


def test_logout_revokes_both_tokens_at_weave_and_deletes_the_file(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["logout"], http=http, say=said.append) == 0

    assert said == ["Signed out."]
    assert store.load() is None
    assert {(r["token"], r["token_type_hint"]) for r in fake.revoked} == {
        ("refresh-1", "refresh_token"),
        ("access-1", "access_token"),
    }
    assert all(r["client_id"] == "cli-app" for r in fake.revoked)


def test_logout_deletes_the_file_and_says_so_when_weave_does_not_revoke(http, fake) -> None:
    _login(http, [])
    fake.revoke_status = 500
    said: list[str] = []

    assert run(["logout"], http=http, say=said.append) == 0

    assert store.load() is None
    assert said[0] == "Signed out." and "revoke" in said[1]


def test_logout_when_not_signed_in_asks_weave_nothing(http, fake) -> None:
    said: list[str] = []

    run(["logout"], http=http, say=said.append)

    assert said == ["Not signed in."] and fake.requests == []


SUBMIT_4090 = [
    "submit",
    PROJECT,
    "--name",
    "train",
    "--image",
    "ghcr.io/me/train:v3",
    "--command",
    "python train.py",
    "--gpu",
    "RTX 4090",
    "--target",
    "0.30",
    "--max",
    "0.45",
]


def test_submit_sends_the_data_estimate(http, fake) -> None:
    _login(http, [])

    run([*SUBMIT_4090, "--data-in", "400", "--data-out", "100"], http=http, say=lambda _: None)

    body = fake.jobs[0]["body"]
    assert (body["data_in_gb"], body["data_out_gb"]) == (400, 100)
    assert "confirm_transfer_cost" not in body


def test_submit_asks_before_a_costly_transfer_and_sends_again_on_yes(http, fake) -> None:
    _login(http, [])
    fake.transfer_confirm = True
    said: list[str] = []
    asked: list[str] = []

    code = run([*SUBMIT_4090, "--data-in", "500"], http=http, say=said.append, prompt=lambda q: asked.append(q) or "y")

    assert code == 0
    assert asked == ["Queue it anyway? [y/N] "]
    assert said[0] == "This job's data transfer could cost up to $10.00."
    assert fake.jobs[-1]["body"]["confirm_transfer_cost"] is True
    assert said[-1] == f"Queued job {JOB}: Queued."


def test_submit_queues_nothing_when_the_member_says_no(http, fake) -> None:
    _login(http, [])
    fake.transfer_confirm = True
    said: list[str] = []

    code = run([*SUBMIT_4090, "--data-in", "500"], http=http, say=said.append, prompt=lambda _: "")

    assert code == 1
    assert [job for job in fake.jobs if "body" in job] == []
    assert said[-1] == "Not queued. --confirm-transfer-cost queues it without asking."


def test_submit_without_a_terminal_reads_no_as_no(http, fake) -> None:
    _login(http, [])
    fake.transfer_confirm = True

    def no_terminal(_: str) -> str:
        raise EOFError

    assert run([*SUBMIT_4090, "--data-in", "500"], http=http, say=lambda _: None, prompt=no_terminal) == 1


def test_confirm_transfer_cost_skips_the_question(http, fake) -> None:
    _login(http, [])
    fake.transfer_confirm = True

    def never(_: str) -> str:
        raise AssertionError("asked")

    code = run(
        [*SUBMIT_4090, "--data-in", "500", "--confirm-transfer-cost"], http=http, say=lambda _: None, prompt=never
    )

    assert code == 0
    assert len(fake.jobs) == 1 and fake.jobs[0]["body"]["confirm_transfer_cost"] is True


def test_login_defaults_to_ganymedes_krater(monkeypatch) -> None:
    monkeypatch.delenv("KRATER_URL", raising=False)
    assert main._parser().parse_args(["login"]).url == main.DEFAULT_URL
    assert main._parser().parse_args(["login", "--url", "https://other"]).url == "https://other"


def test_submit_estimate_only_says_the_price_and_queues_nothing(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    code = run([*SUBMIT_4090, "--data-in", "40", "--estimate"], http=http, say=said.append)

    assert code == 0
    assert fake.estimated[0]["data_in_gb"] == 40
    assert [job for job in fake.jobs if "body" in job] == []
    assert said == ["Up to $4.00 if it runs its full 8 hours."]


def test_an_estimate_waits_for_krater_s_live_price(http, fake, monkeypatch) -> None:
    """Paul, 2026-11-13: the form's prices were an hourly snapshot. Krater's worker now searches Vast for the job's
    shape when asked for an estimate, and the CLI asks for the answer until it comes."""
    _login(http, [])
    original = fake.api
    poll = f"/projects/{PROJECT}/jobs/quotes/abc"
    answers = iter(
        [
            {"status": "checking", "text": "Checking Vast's prices for this job now…"},
            {"status": "ready", "text": "Right now on Vast the cheapest costs $0.5511/h."},
        ]
    )

    def with_live(request, path):
        if path == f"/projects/{PROJECT}/jobs/estimate" and request.method == "POST":
            live = {"status": "checking", "text": "Checking…", "key": "abc", "poll": poll}
            return httpx.Response(200, json={"summary": "Up to $4.00.", "live": live})
        if path == poll:
            return httpx.Response(200, json=next(answers))
        return original(request, path)

    monkeypatch.setattr(fake, "api", with_live)
    said: list[str] = []
    slept: list[float] = []

    assert run([*SUBMIT_4090, "--estimate"], http=http, say=said.append, sleep=slept.append) == 0

    assert said == ["Up to $4.00.", "Right now on Vast the cheapest costs $0.5511/h."]
    assert slept == [2, 2]


def test_an_estimate_stops_waiting_for_the_live_price(http, fake, monkeypatch) -> None:
    _login(http, [])
    original = fake.api
    poll = f"/projects/{PROJECT}/jobs/quotes/abc"

    def never_ready(request, path):
        if path == f"/projects/{PROJECT}/jobs/estimate" and request.method == "POST":
            live = {"status": "checking", "text": "Checking…", "key": "abc", "poll": poll}
            return httpx.Response(200, json={"summary": "Up to $4.00.", "live": live})
        if path == poll:
            return httpx.Response(200, json={"status": "checking", "text": "Checking…"})
        return original(request, path)

    monkeypatch.setattr(fake, "api", never_ready)
    said: list[str] = []
    slept: list[float] = []

    run([*SUBMIT_4090, "--estimate"], http=http, say=said.append, sleep=slept.append)

    assert sum(slept) == main.LIVE_PRICE_WAIT_SECONDS
    assert said[-1].startswith("Krater's live check of Vast's prices didn't answer in time")


def test_submit_says_what_the_job_may_cost(http, fake, monkeypatch) -> None:
    _login(http, [])
    said: list[str] = []
    original = fake.api

    def with_estimate(request, path):
        response = original(request, path)
        if path == f"/projects/{PROJECT}/jobs" and response.status_code == 201:
            return httpx.Response(201, json={**json.loads(response.content), "estimate": {"summary": "Up to $1."}})
        return response

    monkeypatch.setattr(fake, "api", with_estimate)
    run(SUBMIT_4090, http=http, say=said.append)

    assert said[-2:] == [f"Queued job {JOB}: Queued.", "Up to $1."]


# -- Job files, sweeps and rerun ------------------------------------------------------------------------------


def _job_file(tmp_path, text: str) -> str:
    path = tmp_path / "job.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


JOB_TOML = f"""
project = "{PROJECT}"
name = "train"
image = "ghcr.io/me/train:v3"
command = "python train.py"
gpu = ["RTX 4090", "RTX 3090"]
limit = 0.4
max-runtime = 600
disk = 64
"""


def test_a_job_file_gives_the_options(http, fake, tmp_path) -> None:
    _login(http, [])
    assert run(["submit", "-f", _job_file(tmp_path, JOB_TOML)], http=http, say=lambda _: None) == 0
    body = fake.jobs[0]["body"]
    assert body["name"] == "train" and body["gpu_names"] == ["RTX 4090", "RTX 3090"]
    assert body["limit_usd_per_hour"] == "0.4" and body["max_runtime_minutes"] == 600 and body["disk_gb"] == 64
    assert body["num_gpus"] == 1 and body["max_wait_minutes"] == 720  # defaults still apply


def test_the_command_line_wins_over_the_file(http, fake, tmp_path) -> None:
    _login(http, [])
    path = _job_file(tmp_path, JOB_TOML)
    run(["submit", "-f", path, "--max-runtime", "60", "--gpu", "H100", "--name", "short"], http=http, say=print)
    body = fake.jobs[0]["body"]
    assert body["max_runtime_minutes"] == 60 and body["gpu_names"] == ["H100"] and body["name"] == "short"
    assert body["disk_gb"] == 64


def test_a_job_file_without_a_name_says_so(http, fake, tmp_path) -> None:
    _login(http, [])
    said: list[str] = []
    path = _job_file(tmp_path, f'project = "{PROJECT}"\nimage = "x"\nlimit = 0.4\n')
    assert run(["submit", "-f", path], http=http, say=said.append) == 2
    assert said == ["Give --name, here or in a job file (-f)."]
    assert fake.jobs == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('name = "a"\nmax-runtim = 5\n', "The job file has 'max-runtim', which isn't an option of krater submit."),
        ('name = "a"\nmax-runtime = "long"\n', "max-runtime: must be a whole number."),
        ('name = "a"\non-demand = "yes"\n', "on-demand must be true or false."),
        (
            'name = "a"\non-max-wait = "panic"\n',
            "on-max-wait must be one of: keep_waiting, raise_bid, on_demand, cancel.",
        ),
        ("name = [", None),
    ],
)
def test_a_bad_job_file_says_what_is_wrong(http, fake, tmp_path, text, message) -> None:
    _login(http, [])
    said: list[str] = []
    assert run(["submit", "-f", _job_file(tmp_path, text)], http=http, say=said.append) == 2
    if message is not None:
        assert said == [message]
    else:
        assert "isn't valid TOML" in said[0]


def test_a_sweep_queues_one_job_per_combination(http, fake, tmp_path) -> None:
    _login(http, [])
    said: list[str] = []
    path = _job_file(tmp_path, JOB_TOML + "\n[sweep]\nlr = [0.1, 0.01]\n")

    code = run(["submit", "-f", path, "--sweep", "seed=1,2"], http=http, say=said.append)

    assert code == 0
    bodies = [job["body"] for job in fake.jobs]
    assert [b["name"] for b in bodies] == ["train-0", "train-1", "train-2", "train-3"]
    assert bodies[0]["command"] == "export KRATER_SWEEP_INDEX=0 lr=0.1 seed=1; python train.py"
    assert bodies[3]["command"] == "export KRATER_SWEEP_INDEX=3 lr=0.01 seed=2; python train.py"
    assert said[0] == "Queued 4 jobs:" and said[1].endswith("train-0  lr=0.1 seed=1")


def test_count_queues_copies_with_an_index_and_quotes_values(http, fake) -> None:
    _login(http, [])
    run([*SUBMIT_4090, "--count", "2", "--sweep", "msg=a b"], http=http, say=lambda _: None)
    commands = [job["body"]["command"] for job in fake.jobs]
    assert commands == [
        "export KRATER_SWEEP_INDEX=0 msg='a b'; python train.py",
        "export KRATER_SWEEP_INDEX=1 msg='a b'; python train.py",
    ]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--count", "101"], "That sweep is 101 jobs; one submit queues at most 100."),
        (
            ["--sweep", "PATH=/x"],
            "Can't sweep 'PATH': use a name of letters, digits and _ that isn't PATH, HOME or KRATER_...",
        ),
        (["--sweep", "lr"], "--sweep lr: give it as name=value1,value2."),
        (["--count", "0"], "--count must be 1 or more."),
    ],
)
def test_a_bad_sweep_queues_nothing(http, fake, extra, message) -> None:
    _login(http, [])
    said: list[str] = []
    assert run([*SUBMIT_4090, *extra], http=http, say=said.append) == 2
    assert said == [message] and fake.jobs == []


def test_a_box_cant_be_swept(http, fake) -> None:
    _login(http, [])
    said: list[str] = []
    argv = ["submit", PROJECT, "--name", "nb", "--image", "x", "--box", "--limit", "0.3", "--count", "2"]
    assert run(argv, http=http, say=said.append) == 2
    assert said == ["A sweep is for jobs, not boxes."]


def test_a_sweep_estimate_says_the_total(http, fake) -> None:
    _login(http, [])
    said: list[str] = []
    assert run([*SUBMIT_4090, "--count", "3", "--estimate"], http=http, say=said.append) == 0
    assert said == ["3 jobs. Each: Up to $4.00 if it runs its full 8 hours. All of them: up to $12.00."]
    assert fake.jobs == []


def test_a_sweep_asks_about_the_transfer_once(http, fake) -> None:
    _login(http, [])
    fake.transfer_confirm = True
    asked: list[str] = []

    code = run([*SUBMIT_4090, "--count", "3"], http=http, say=lambda _: None, prompt=lambda q: asked.append(q) or "y")

    assert code == 0 and asked == ["Queue them anyway? [y/N] "]
    assert len([job for job in fake.jobs if "body" in job]) == 3


def test_a_sweep_refused_part_way_says_what_was_queued(http, fake, monkeypatch) -> None:
    _login(http, [])
    said: list[str] = []
    original = fake.api
    posts = []

    def refuse_the_second(request, path):
        if path == f"/projects/{PROJECT}/jobs" and request.method == "POST":
            posts.append(1)
            if len(posts) == 2:
                return httpx.Response(409, json={"error": "The budget is spent."})
        return original(request, path)

    monkeypatch.setattr(fake, "api", refuse_the_second)
    with pytest.raises(KraterError, match="The budget is spent."):
        run([*SUBMIT_4090, "--count", "3"], http=http, say=said.append)
    assert said[0] == "Queued 1 of 3 jobs before Krater refused train-1:"
    assert len(posts) == 2


def test_rerun_queues_the_same_settings(http, fake) -> None:
    _login(http, [])
    spec = {"name": "train", "image": "x", "command": "go", "kind": "job", "limit_usd_per_hour": "0.4000"}
    fake.details[JOB_C] = {**job_row(JOB_C, "train"), "spec": spec}
    fake.project_jobs[PROJECT].append(job_row(JOB_C, "train"))
    said: list[str] = []

    assert run(["rerun", JOB_C, "--name", "train-again"], http=http, say=said.append) == 0

    assert fake.jobs[0]["body"] == {**spec, "name": "train-again"}
    assert said == [f"Queued job {JOB}: Queued."]


def test_rerun_without_settings_from_an_older_krater_says_so(http, fake) -> None:
    _login(http, [])
    fake.details[JOB_C] = job_row(JOB_C, "train")
    said: list[str] = []
    assert run(["rerun", JOB_C], http=http, say=said.append) == 1
    assert said == ["This Krater doesn't send a job's settings yet, so it can't be run again from here."]


# -- Inputs ---------------------------------------------------------------------------------------------------


def test_inputs_upload_sends_files_and_folders_to_presigned_urls(http, fake, tmp_path) -> None:
    _login(http, [])
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "a.csv").write_bytes(b"1,2\n")
    (data / "sub" / "b.csv").write_bytes(b"3,4\n")
    single = tmp_path / "vocab.txt"
    single.write_bytes(b"hello")
    said: list[str] = []

    assert run(["inputs", "upload", PROJECT, str(data), str(single)], http=http, say=said.append) == 0

    assert fake.put_bodies == {"/put/data/a.csv": b"1,2\n", "/put/data/sub/b.csv": b"3,4\n", "/put/vocab.txt": b"hello"}
    assert said == ["Uploaded data/a.csv (4 B)", "Uploaded data/sub/b.csv (4 B)", "Uploaded vocab.txt (5 B)"]


def test_inputs_upload_skips_what_is_already_there_and_names_one_with_as(http, fake, tmp_path) -> None:
    _login(http, [])
    fake.inputs = {"train.bin": 3}
    local = tmp_path / "x.bin"
    local.write_bytes(b"abc")
    said: list[str] = []

    run(["inputs", "upload", PROJECT, str(local), "--as", "train.bin"], http=http, say=said.append)

    assert fake.put_bodies == {} and said == ["1 already there at the same size, skipped."]


def test_a_big_input_goes_up_in_parts(http, fake, tmp_path) -> None:
    _login(http, [])
    fake.input_part_bytes = 4
    local = tmp_path / "big.bin"
    local.write_bytes(b"0123456789")

    run(["inputs", "upload", PROJECT, str(local)], http=http, say=lambda _: None)

    assert fake.put_bodies == {"/part/big.bin/1": b"0123", "/part/big.bin/2": b"4567", "/part/big.bin/3": b"89"}
    assert fake.completed == [{"name": "big.bin", "upload_id": "u1", "etags": ['"etag-4"', '"etag-4"', '"etag-2"']}]


def test_inputs_list_and_rm(http, fake) -> None:
    _login(http, [])
    fake.inputs = {"data/a.csv": 2048, "data/b.csv": 10, "vocab.txt": 5}
    said: list[str] = []

    run(["inputs", "list", PROJECT], http=http, say=said.append)
    run(["inputs", "rm", PROJECT, "data/"], http=http, say=said.append)

    assert said[0].split() == ["NAME", "SIZE"] and "data/a.csv" in said[1]
    assert said[-1] == "Deleted 2 input files."
    assert fake.inputs == {"vocab.txt": 5}


def test_submit_names_its_inputs(http, fake, tmp_path) -> None:
    _login(http, [])
    run([*SUBMIT_4090, "--input", "data/", "--input", "vocab.txt"], http=http, say=lambda _: None)
    assert fake.jobs[0]["body"]["inputs"] == ["data/", "vocab.txt"]

    path = _job_file(tmp_path, JOB_TOML + 'input = "data/"\n')
    run(["submit", "-f", path], http=http, say=lambda _: None)
    assert fake.jobs[1]["body"]["inputs"] == ["data/"]


# -- Smoke tests (`krater submit --smoke`) ---------------------------------------------------------------------


def test_submit_smoke_asks_for_a_smoke_test_and_changes_nothing_else(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run([*SUBMIT_4090, "--smoke"], http=http, say=said.append) == 0

    body = fake.jobs[0]["body"]
    assert body["smoke_minutes"] == 5 and body["max_runtime_minutes"] == 480
    # The full run's wait: `rerun --no-smoke` posts this body back, and a limit order there may wait for hours.
    assert body["max_wait_minutes"] == 720 and body["on_max_wait"] == "keep_waiting"
    assert said[-1] == (
        "A smoke test: its command stops after 5 minutes of running. Once it passes, "
        f"`krater rerun {JOB[:8]} --no-smoke` queues the full run."
    )


def test_submit_smoke_minutes_alone_asks_for_a_smoke_test(http, fake) -> None:
    _login(http, [])
    run([*SUBMIT_4090, "--smoke-minutes", "1"], http=http, say=lambda _: None)
    assert fake.jobs[0]["body"]["smoke_minutes"] == 1


def test_a_smoke_test_of_a_sweep_queues_only_its_first_job(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run([*SUBMIT_4090, "--sweep", "lr=0.1,0.01", "--smoke"], http=http, say=said.append) == 0

    assert [job["body"]["name"] for job in fake.jobs] == ["train-0"]
    assert said[0] == "A smoke test runs only the first of the sweep's 2 jobs (train-0)."
    assert said[-1].endswith("Once it passes, the same `krater submit` without --smoke queues the whole sweep.")


def test_a_box_cant_be_a_smoke_test(http, fake) -> None:
    _login(http, [])
    said: list[str] = []
    assert run([*SUBMIT_4090, "--box", "--smoke"], http=http, say=said.append) == 2
    assert fake.jobs == [] and said == ["A smoke test is for jobs: a box already runs until you stop it."]


def test_an_older_krater_that_ignores_the_smoke_test_has_the_job_cancelled(http, fake) -> None:
    _login(http, [])
    fake.knows_smoke = False
    fake.details[JOB] = {**job_row(JOB, "train"), "state": "cancelled"}

    with pytest.raises(KraterError, match="doesn't run smoke tests yet"):
        run([*SUBMIT_4090, "--smoke"], http=http, say=lambda _: None)

    assert any(r.url.path == f"/api/v1/jobs/{JOB}/cancel" and r.method == "POST" for r in fake.requests)


def test_rerun_no_smoke_queues_the_full_run(http, fake) -> None:
    _login(http, [])
    spec = {"name": "t", "image": "x", "command": "go", "kind": "job", "limit_usd_per_hour": "0.4", "smoke_minutes": 5}
    fake.details[JOB_C] = {**job_row(JOB_C, "train"), "spec": spec}

    assert run(["rerun", JOB_C, "--no-smoke"], http=http, say=lambda _: None) == 0

    assert "smoke_minutes" not in fake.jobs[0]["body"]


def test_rerun_smoke_makes_it_a_smoke_test(http, fake) -> None:
    _login(http, [])
    spec = {"name": "train", "image": "x", "command": "go", "kind": "job", "limit_usd_per_hour": "0.4"}
    fake.details[JOB_C] = {**job_row(JOB_C, "train"), "spec": spec}

    assert run(["rerun", JOB_C, "--smoke"], http=http, say=lambda _: None) == 0

    assert fake.jobs[0]["body"]["smoke_minutes"] == 5


def test_jobs_marks_smoke_tests(http, fake) -> None:
    _login(http, [])
    fake.project_jobs[PROJECT] = [{**job_row(JOB, "train", state="succeeded"), "smoke_minutes": 5}]
    said: list[str] = []
    run(["jobs", PROJECT], http=http, say=said.append)
    assert "succeeded (smoke test)" in said[1]


# -- Local runs (`krater run`) ---------------------------------------------------------------------------------


class Docker:
    """Stands in for `local.execute`: records what would run, and answers with an outcome."""

    def __init__(self, outcome=None, writes: dict[str, str] | None = None) -> None:
        self.runs: list = []
        self.outcome = outcome or main.local.Outcome(code=0)
        self.writes = writes or {}

    def __call__(self, spec):
        self.runs.append(spec)
        for name, text in self.writes.items():
            path = os.path.join(spec.folder, "outputs", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return self.outcome


def _run_local(argv, http, docker, said, which=lambda name: f"/usr/bin/{name}"):
    return run(["run", *argv], http=http, say=said.append, run_local=docker, which=which)


def test_run_runs_the_job_here_without_signing_in(http, fake, tmp_path) -> None:
    docker = Docker(writes={"score.txt": "0.93"})
    said: list[str] = []
    folder = str(tmp_path / "out")

    code = _run_local(
        ["--image", "python:3.12-slim", "--command", "python -c 'print(1)'", "--name", "t", "--dir", folder],
        http,
        docker,
        said,
    )

    assert code == 0 and fake.requests == []  # no Krater, no Weave
    spec = docker.runs[0]
    assert (spec.image, spec.command, spec.folder, spec.smoke_minutes) == (
        "python:3.12-slim",
        "python -c 'print(1)'",
        folder,
        None,
    )
    assert said[-1] == "Finished: exit code 0. It wrote 1 output file (4 B)."


def test_run_reads_a_job_file_and_runs_the_chosen_sweep_job(http, fake, tmp_path) -> None:
    docker = Docker()
    said: list[str] = []
    path = _job_file(tmp_path, JOB_TOML.replace(f'project = "{PROJECT}"\n', "") + "\n[sweep]\nlr = [0.1, 0.01]\n")

    code = _run_local(["-f", path, "--index", "1", "--dir", str(tmp_path / "o")], http, docker, said)

    assert code == 0
    assert docker.runs[0].command == "export KRATER_SWEEP_INDEX=1 lr=0.01; python train.py"
    assert said[0] == "Running job 1 of the sweep's 2 (lr=0.01); --index picks another."


def test_run_explains_a_failure_it_knows(http, fake, tmp_path) -> None:
    docker = Docker(outcome=main.local.Outcome(code=127))
    said: list[str] = []
    code = _run_local(["--image", "x", "--command", "trian.py", "--dir", str(tmp_path)], http, docker, said)
    assert code == 127
    assert said[-2:] == [
        "Failed: exit code 127.",
        "A command wasn't found in the image (exit 127): check the command, and that the image has it installed.",
    ]


def test_run_says_a_smoke_test_passed(http, fake, tmp_path) -> None:
    docker = Docker(outcome=main.local.Outcome(code=0, smoke_passed=True))
    said: list[str] = []
    code = _run_local(["--image", "x", "--command", "go", "--smoke", "--dir", str(tmp_path)], http, docker, said)
    assert code == 0 and docker.runs[0].smoke_minutes == 5
    assert said[-1] == "Smoke test passed: the command ran 5 minutes without failing."


def test_run_without_docker_says_how_to_get_it(http, fake) -> None:
    said: list[str] = []
    code = _run_local(["--image", "x", "--command", "go"], http, Docker(), said, which=lambda name: None)
    assert code == 1 and said[-1].startswith("No docker command found. Install Docker Desktop")


def test_run_needs_an_input_folder_for_project_inputs(http, fake) -> None:
    said: list[str] = []
    code = _run_local(["--image", "x", "--command", "go", "--input", "data/"], http, Docker(), said)
    assert code == 2 and "--input-dir FOLDER" in said[-1]


def test_run_passes_the_project_secrets_set_in_this_shell_by_name(http, fake, tmp_path, monkeypatch) -> None:
    _login(http, [])
    fake.secrets = {"HF_TOKEN": "x", "WANDB_KEY": "y"}
    monkeypatch.setenv("HF_TOKEN", "hf_local")
    monkeypatch.delenv("WANDB_KEY", raising=False)
    docker = Docker()
    said: list[str] = []

    _run_local([PROJECT, "--image", "x", "--command", "go", "--dir", str(tmp_path)], http, docker, said)

    assert docker.runs[0].env_names == ["HF_TOKEN"]
    assert "Not set in this shell, so the job won't have them (Krater can't send their values here): WANDB_KEY." in said
    args = docker.runs[0].docker_args("c")
    assert "hf_local" not in " ".join(args) and args[args.index("HF_TOKEN") - 1] == "-e"


def test_run_refuses_a_box(http, fake) -> None:
    said: list[str] = []
    assert _run_local(["--image", "x", "--box"], http, Docker(), said) == 2
    assert said == [
        "A box is an SSH machine with no command to run. To try its image here: docker run -it --rm --entrypoint sh x"
    ]


def test_the_docker_command_matches_a_krater_machine(tmp_path) -> None:
    spec = main.local.LocalRun(
        image="ghcr.io/me/train:v3",
        command="python train.py",
        name="train",
        folder=str(tmp_path),
        input_dir=str(tmp_path / "data"),
        smoke_minutes=5,
        gpu=True,
    )

    args = spec.docker_args("krater-local-train-abc")

    assert args[:9] == [
        "docker",
        "run",
        "--rm",
        "--name",
        "krater-local-train-abc",
        "--platform",
        "linux/amd64",
        "--entrypoint",
        "sh",
    ]
    assert args[-3:] == ["ghcr.io/me/train:v3", "-c", "python train.py"]
    pairs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    assert {"KRATER_RESUMED=0", "KRATER_SMOKE=1", "KRATER_LOCAL=1", "KRATER_OUTPUT_DIR=/krater/outputs"} <= set(pairs)
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert mounts[-1] == f"{tmp_path / 'data'}:/krater/inputs:ro"
    assert args[args.index("--gpus") + 1] == "all"


class FakeProcess:
    def __init__(self, code: int, *, interrupt: bool = False) -> None:
        self.code, self.interrupt, self.waits = code, interrupt, 0

    def wait(self):
        self.waits += 1
        if self.interrupt and self.waits == 1:
            raise KeyboardInterrupt
        return self.code


class FiringTimer:
    """A timer that fires as soon as it's started: the smoke test's minutes are up at once."""

    def __init__(self, seconds, fn) -> None:
        self.seconds, self.fn, self.daemon = seconds, fn, False

    def start(self) -> None:
        self.fn()

    def cancel(self) -> None:
        pass


def test_execute_stops_a_smoke_test_with_docker_stop_and_calls_it_passed(tmp_path) -> None:
    calls: list[list[str]] = []
    spec = main.local.LocalRun(image="x", command="go", name="t", folder=str(tmp_path), smoke_minutes=2)

    outcome = main.local.execute(
        spec,
        popen=lambda args: FakeProcess(143),
        call=lambda args: calls.append(list(args)) or 0,
        timer=FiringTimer,
    )

    assert outcome.smoke_passed and outcome.code == 0
    assert calls[0][:4] == ["docker", "stop", "-t", "30"] and calls[0][4].startswith("krater-local-t-")
    assert (tmp_path / "outputs").is_dir() and (tmp_path / "checkpoints").is_dir()


def test_execute_reports_the_commands_own_exit_and_stops_on_ctrl_c(tmp_path) -> None:
    spec = main.local.LocalRun(image="x", command="go", name="t", folder=str(tmp_path))
    assert main.local.execute(spec, popen=lambda args: FakeProcess(3), call=lambda args: 0).code == 3

    calls: list = []
    outcome = main.local.execute(
        spec, popen=lambda args: FakeProcess(143, interrupt=True), call=lambda args: calls.append(args) or 0
    )
    assert outcome.interrupted and outcome.code == 130 and calls[0][1] == "stop"
