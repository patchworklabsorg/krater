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

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://store.test/metrics/"):
            return httpx.Response(200, content=json.dumps(self.metrics[url.rsplit("/", 1)[1]]).encode())
        return super().handler(request)

    def api(self, request: httpx.Request, path: str) -> httpx.Response:
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
    assert [row.split()[0] for row in rows] == ["eval.acc", "loss", "steps"]
    assert rows[1].split()[1:] == ["0.123457", "0.2"] and rows[2].split()[1:] == ["-", "100"]


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
