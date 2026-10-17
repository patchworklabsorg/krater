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
        self.secrets: dict[str, str] = {}
        self.jobs_422 = False
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
        if path == f"/projects/{PROJECT}/jobs" and request.method == "POST":
            body = json.loads(request.content)
            job = {"id": JOB, "kind": body["kind"], "state": "queued", "state_reason": "Queued.", "body": body}
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

    assert [line.split()[0:3] for line in said[1:]] == [
        ["RTX", "3060", "interruptible"],
        ["RTX", "3060", "on"],
        ["RTX", "4090", "interruptible"],
        ["RTX", "4090", "on"],
    ]
    assert "on demand" in said[2] and "H100" not in "".join(said) and "ondemand" not in "".join(said)


def test_prices_all_and_gpu_filters(http, fake) -> None:
    _login(http, [])
    _prices(fake)
    said: list[str] = []

    run(["prices", "--all"], http=http, say=said.append)
    assert len(said) == 6 and said[-1].startswith("H100") and said[-1].split()[-3:] == ["-", "-", "0"]
    said.clear()
    run(["prices", "--gpu", "4090"], http=http, say=said.append)
    assert len(said) == 3 and all("4090" in line for line in said[1:])
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
    assert said[0] == "Right now, per hour, interruptible:"
    assert said[2].split() == ["RTX", "3060", "$0.0700", "$0.0900", "12"] and len(said) == 4
    assert said[-1] == GIVE_PRICES
    said.clear()
    assert run([*SUBMIT, "--target", "0.07", "--on-demand"], http=http, say=said.append) == 2
    assert said[0] == "Right now, per hour, on demand:" and "$0.1000" in said[2]


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
    assert sleeps == [5, 10, 20, 40]  # a growing pause, starting at the interval
    assert capsys.readouterr().err.count("trying again") == 1


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

    assert code == 0 and said == ["a", "nb: succeeded", "sleeps=[5, 10]"]


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
