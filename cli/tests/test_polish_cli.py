"""`--max-spend`, `submit --follow`, `compare`, `logs` naming an attempt with no output yet, and `run` without Krater."""

from __future__ import annotations

import json

import httpx
import pytest
from test_cli import JOB, JOB_C, PROJECT, SUBMIT, Docker, Fake, _login, _run_local

from krater_cli.api import KraterError
from krater_cli.main import run


class PolishFake(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.metrics: dict[str, object] = {}
        self.latest_attempt: int | None = None
        self.limits: list[dict] = []
        self.cli_latest: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://store.test/metrics/"):
            return httpx.Response(200, content=json.dumps(self.metrics[url.rsplit("/", 1)[1]]).encode())
        response = super().handler(request)
        if self.cli_latest and str(request.url).startswith("https://krater.test/api/v1/"):
            response.headers["X-Krater-CLI-Latest"] = self.cli_latest
        return response

    def api(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == f"/jobs/{JOB}/limits" and request.method == "POST":
            body = json.loads(request.content)
            self.limits.append(body)
            changed = (
                [f"max runtime 480 → {body['max_runtime_minutes']} minutes"] if "max_runtime_minutes" in body else []
            )
            return httpx.Response(200, json={**self.details[JOB], "changed": changed})
        for job_id in self.metrics:
            if path == f"/jobs/{job_id}/files":
                file = {"area": "outputs", "name": "metrics.json", "size_bytes": 9}
                return httpx.Response(200, json={"files": [{**file, "url": f"https://store.test/metrics/{job_id}"}]})
        response = super().api(request, path)
        if path.startswith(f"/jobs/{JOB}/logs") and self.latest_attempt is not None:
            return httpx.Response(200, json={**response.json(), "latest_attempt": self.latest_attempt})
        return response


@pytest.fixture
def fake() -> PolishFake:
    return PolishFake()


@pytest.fixture
def http(fake: PolishFake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def test_max_spend_is_sent_in_cents(http, fake) -> None:
    _login(http, [])
    assert run([*SUBMIT, "--limit", "0.1", "--max-spend", "$12.5"], http=http, say=lambda _: None) == 0
    assert fake.jobs[-1]["body"]["max_spend_cents"] == 1250


def test_max_spend_on_a_krater_without_spend_caps_cancels_the_job(http, fake) -> None:
    _login(http, [])
    fake.knows_max_spend = False
    with pytest.raises(KraterError, match="doesn't take --max-spend yet"):
        run([*SUBMIT, "--limit", "0.1", "--max-spend", "5"], http=http, say=lambda _: None)

    assert any(r.url.path == f"/api/v1/jobs/{JOB}/cancel" for r in fake.requests)


def test_max_spend_must_be_an_amount(http, fake, capsys) -> None:
    with pytest.raises(SystemExit):
        run([*SUBMIT, "--limit", "0.1", "--max-spend", "0"], http=http, say=lambda _: None)
    assert "isn't an amount in dollars" in capsys.readouterr().err


def test_submit_follow_prints_the_output_then_downloads_the_outputs(http, fake, tmp_path, monkeypatch) -> None:
    _login(http, [])
    monkeypatch.chdir(tmp_path)
    fake.details[JOB].update(kind="job", state="running")
    fake.log_files = {1: b"epoch 1\n"}

    def finish(_seconds: float) -> None:
        fake.log_files[1] += b"done\n"
        fake.details[JOB].update(state="succeeded", exit_code=0)

    said: list[str] = []
    code = run([*SUBMIT, "--limit", "0.1", "--follow"], http=http, say=said.append, sleep=finish)

    assert code == 0
    assert "epoch 1" in said and "done" in said
    assert (tmp_path / "outputs" / "eval" / "score.txt").read_bytes() == b"0.93\n"
    assert not (tmp_path / "logs").exists()  # only the outputs


def test_submit_follow_exits_with_a_failed_jobs_code_and_downloads_nothing(http, fake, tmp_path, monkeypatch) -> None:
    _login(http, [])
    monkeypatch.chdir(tmp_path)
    fake.details[JOB].update(kind="job", state="failed", exit_code=3)
    fake.log_files = {1: b"Traceback\n"}

    assert run([*SUBMIT, "--limit", "0.1", "--follow"], http=http, say=lambda _: None) == 3
    assert not (tmp_path / "outputs").exists()


def test_submit_follow_is_for_one_job(http, fake) -> None:
    _login(http, [])
    said: list[str] = []
    assert (
        run([*SUBMIT, "--limit", "0.1", "--follow", "--count", "2", "--command", "go"], http=http, say=said.append) == 2
    )
    assert said == ["--follow is for one job: not a box, a sweep, --estimate or --json."]
    assert fake.jobs == []


def test_compare_shows_each_jobs_metrics_side_by_side(http, fake) -> None:
    _login(http, [])
    fake.details[JOB_C] = {**fake.details[JOB], "id": JOB_C, "name": "sing"}
    fake.metrics = {JOB: {"loss": 0.1234567, "eval": {"acc": 0.9}}, JOB_C: {"loss": 0.2, "steps": 100}}
    said: list[str] = []

    assert run(["compare", JOB, JOB_C], http=http, say=said.append) == 0

    header, *rows = said
    assert "nb (22222222)" in header and "sing (9f9f9f9f)" in header
    assert [row.split()[0] for row in rows] == ["state", "cost", "runtime", "gpu", "eval.acc", "loss", "steps"]
    assert rows[5].split()[1:] == ["0.123457", "0.2"] and rows[6].split()[1:] == ["-", "100"]


def test_compare_says_when_there_is_nothing_to_compare(http, fake) -> None:
    _login(http, [])
    said: list[str] = []
    assert run(["compare", JOB], http=http, say=said.append) == 1
    assert said[0].startswith("nb (22222222): no outputs/metrics.json")


def test_logs_says_when_the_newest_attempt_has_no_output_yet(http, fake) -> None:
    _login(http, [])
    fake.log_files = {1: b"epoch 9\n"}
    fake.latest_attempt = 2
    said: list[str] = []

    assert run(["logs", JOB], http=http, say=said.append) == 0

    assert said == ["[attempt 2 has no output yet; showing attempt 1]", "epoch 9"]


def test_run_without_signing_in_says_so_plainly(http, fake, tmp_path) -> None:
    said: list[str] = []
    _run_local([PROJECT, "--image", "x", "--command", "go", "--dir", str(tmp_path)], http, Docker(), said)
    assert (
        "Running without the project's secrets: couldn't read their names, as you're not signed in (krater login)."
    ) in said


def test_compare_shows_cost_runtime_and_gpu(http, fake) -> None:
    _login(http, [])
    fake.details[JOB].update(
        spend_cents=1234,
        started_at="2026-11-19T10:00:00+00:00",
        ended_at="2026-11-19T11:30:00+00:00",
        state="succeeded",
        gpu_names=["RTX 3090"],
        machine=None,
    )
    fake.metrics = {JOB: {"loss": 0.5}}
    said: list[str] = []

    run(["compare", JOB], http=http, say=said.append)

    rows = {row.split()[0]: row.split()[1:] for row in said[1:]}
    assert rows["state"] == ["succeeded"] and rows["cost"] == ["$12.34"]
    assert rows["runtime"] == ["1h30m"] and rows["gpu"] == ["RTX", "3090"]


def test_compare_takes_a_whole_sweep(http, fake) -> None:
    _login(http, [])
    fake.project_jobs[PROJECT] = [
        {**fake.project_jobs[PROJECT][0], "id": JOB, "name": "lr-1"},
        {**fake.project_jobs[PROJECT][0], "id": JOB_C, "name": "lr-0"},
        {**fake.project_jobs[PROJECT][0], "id": "77777777-7777-7777-7777-777777777777", "name": "lr-final"},
    ]
    fake.details[JOB_C] = {**fake.details[JOB], "id": JOB_C, "name": "lr-0"}
    fake.metrics = {JOB: {"loss": 0.1}, JOB_C: {"loss": 0.2}}
    said: list[str] = []

    assert run(["compare", "--sweep", "lr", "--project", PROJECT], http=http, say=said.append) == 0

    assert said[0].split()[0:2] == ["sing", "(9f9f9f9f)"] or "(9f9f9f9f)" in said[0]
    assert "lr-final" not in said[0]


def test_limits_sends_the_changes(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["limits", JOB, "--max-runtime", "600", "--max-spend", "none"], http=http, say=said.append) == 0

    assert fake.limits == [{"max_runtime_minutes": 600, "max_spend_cents": None}]
    assert said == ["Changed: max runtime 480 → 600 minutes."]


def test_limits_needs_something_to_change(http, fake) -> None:
    _login(http, [])
    said: list[str] = []
    assert run(["limits", JOB], http=http, say=said.append) == 2


def test_a_newer_cli_is_mentioned_once_a_day(http, fake, capsys, monkeypatch) -> None:
    from krater_cli import newer

    monkeypatch.setattr(newer, "_said", False)
    _login(http, [])
    fake.cli_latest = "99.0.0"

    run(["whoami"], http=http, say=lambda _: None)
    assert "version 99.0.0 is out" in capsys.readouterr().err

    monkeypatch.setattr(newer, "_said", False)
    run(["whoami"], http=http, say=lambda _: None)
    assert capsys.readouterr().err == ""  # not again within the day

    assert not newer.is_newer("0.1.0") and not newer.is_newer("garbage") and newer.is_newer("100.0")


def test_code_that_builds_cuda_is_warned_of_on_a_runtime_image(http, fake, tmp_path) -> None:
    from test_cli import SUBMIT_CODE, _code_folder

    _login(http, [])
    files = {
        "train.py": "from torch.utils.cpp_extension import load_inline\n",
        "requirements.txt": "flash-attn==2.6.3\nnumpy\n",
    }
    folder = _code_folder(tmp_path, files)
    said: list[str] = []

    assert run([*SUBMIT_CODE, "--code", folder, "--limit", "0.4"], http=http, say=said.append) == 0

    (warning,) = [line for line in said if "builds CUDA code" in line]
    assert "flash-attn in requirements.txt" in warning and "cpp_extension in train.py" in warning
    assert "-devel" in warning
