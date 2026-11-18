"""`krater submit --workspace`, `krater end`, `krater files` and `krater workspaces` against the fake Krater."""

from __future__ import annotations

import os
import sys

import httpx
import pytest
from test_cli import JOB, PROJECT, Fake, _login, _show_ready

from krater_cli.api import KraterError
from krater_cli.main import run

WS = "77777777-7777-7777-7777-777777777777"
WS_B = "78787878-7777-7777-7777-777777777777"
MTIME = 1_763_000_000_000_000_000


class WorkspaceFake(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.workspaces = [
            {
                "id": WS,
                "name": "llm",
                "created_at": "2026-11-17T01:00:00+00:00",
                "last_saved_at": "2026-11-17T02:00:00+00:00",
                "files": 3,
                "bytes": 2048,
                "holder": None,
            },
            {
                "id": WS_B,
                "name": "busy",
                "created_at": "2026-11-17T01:00:00+00:00",
                "last_saved_at": None,
                "files": None,
                "bytes": None,
                "holder": {"id": JOB, "name": "nb", "state": "running"},
            },
        ]
        self.entries = [
            {"path": "venv", "type": "dir", "mode": 0o755, "mtime_ns": 0, "size_bytes": 0, "target": None, "url": None},
            {
                "path": "venv/bin/run.sh",
                "type": "file",
                "mode": 0o755,
                "mtime_ns": MTIME,
                "size_bytes": 4,
                "target": None,
                "url": "https://store.test/ws/ckpt",
            },
            {
                "path": "venv/bin/python",
                "type": "link",
                "mode": 0,
                "mtime_ns": 0,
                "size_bytes": 0,
                "target": "/usr/bin/python3",
                "url": None,
            },
            {
                "path": "../escape.txt",
                "type": "file",
                "mode": 0o644,
                "mtime_ns": 0,
                "size_bytes": 1,
                "target": None,
                "url": "https://store.test/ws/score",
            },
        ]
        self.deleted: list[str] = []

    def api(self, request: httpx.Request, path: str) -> httpx.Response:
        base = f"/projects/{PROJECT}/workspaces"
        if path == base:
            return httpx.Response(200, json={"workspaces": self.workspaces})
        if path == f"{base}/{WS}/files":
            return httpx.Response(
                200, json={"workspace": self.workspaces[0], "has_manifest": True, "entries": self.entries}
            )
        if path.startswith(f"{base}/") and request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"deleted": 3})
        if path == f"/projects/{PROJECT}/files":
            return httpx.Response(
                200,
                json={
                    "quota_bytes": 50 * 1024**3,
                    "total": {"files": 5, "bytes": 3000},
                    "inputs": {"files": 1, "bytes": 900},
                    "other": {"files": 0, "bytes": 0},
                    "workspaces": [{**self.workspaces[0], "stored": {"files": 3, "bytes": 2048}}],
                    "jobs": [
                        {
                            "id": JOB,
                            "name": "nb",
                            "kind": "job",
                            "state": "succeeded",
                            "areas": {"outputs": {"files": 1, "bytes": 52}},
                        }
                    ],
                },
            )
        return super().api(request, path)


@pytest.fixture
def fake() -> WorkspaceFake:
    return WorkspaceFake()


@pytest.fixture
def http(fake: WorkspaceFake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


SUBMIT = ["submit", PROJECT, "--name", "nb2", "--image", "ubuntu:24.04", "--box", "--gpu", "RTX 3060"]
PRICE = ["--target", "0.07", "--max", "0.10"]


def test_submit_continues_a_workspace(http, fake) -> None:
    _login(http, [])

    assert run([*SUBMIT, *PRICE, "--workspace", "llm", "--end-holder"], http=http, say=lambda _: None) == 0

    body = fake.jobs[-1]["body"]
    assert body["workspace"] == "llm" and body["end_holder"] is True and "workspace_name" not in body


def test_submit_names_a_new_workspace(http, fake) -> None:
    _login(http, [])

    run([*SUBMIT, *PRICE, "--new-workspace", "finetune"], http=http, say=lambda _: None)

    assert fake.jobs[-1]["body"]["workspace_name"] == "finetune"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--workspace", "llm"], "are for a box"),
        (["--box", "--end-holder"], "goes with --workspace"),
    ],
)
def test_workspace_options_are_checked(http, fake, extra, message) -> None:
    _login(http, [])
    said: list[str] = []
    argv = ["submit", PROJECT, "--name", "x", "--image", "i", "--gpu", "RTX 3060", *PRICE, *extra]

    assert run(argv, http=http, say=said.append) == 2

    assert message in said[-1]


def test_end_asks_krater_to_save_then_destroy(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["end", JOB], http=http, say=said.append) == 0

    assert any(r.method == "POST" and r.url.path.endswith(f"/jobs/{JOB}/end") for r in fake.requests)
    assert said[-1].startswith("nb: ending. Its /workspace is saved first")


def test_show_on_an_ended_box_says_its_workspace_stays(http, fake) -> None:
    _login(http, [])
    _show_ready(fake, JOB)
    fake.details[JOB].update(
        state="cancelled",
        workspace={"id": WS, "name": "llm"},
        machine=None,
        box={
            "destroy_at": None,
            "destroy_after_hours": 24,
            "workspace_saved_at": "2026-11-13T09:29:00+00:00",
            "workspace_save_minutes": 10,
        },
    )
    said: list[str] = []

    run(["show", JOB], http=http, say=said.append)

    assert any(
        line.startswith("  destroyed: its machine and disk are gone; its workspace 'llm' keeps") for line in said
    )


def test_files_prints_the_quota_workspaces_and_jobs(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["files", PROJECT], http=http, say=said.append) == 0

    assert said[0] == "2.9 KB of 50.0 GB used (0%), 5 files."
    assert any("llm" in line and "2.0 KB" in line for line in said)
    assert any("nb" in line and "52 B" in line for line in said)


def test_workspaces_list(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    run(["workspaces", "list", PROJECT], http=http, say=said.append)

    assert said[0].split() == ["ID", "NAME", "LAST", "SAVED", "SIZE", "IN", "USE", "BY"]
    assert "nb (running)" in said[2] and "never" in said[2]


def test_workspaces_download_restores_folders_modes_times_and_symlinks(http, fake, tmp_path) -> None:
    _login(http, [])
    said: list[str] = []
    target = tmp_path / "ws"

    assert run(["workspaces", "download", PROJECT, "llm", str(target)], http=http, say=said.append) == 0

    script = target / "venv" / "bin" / "run.sh"
    assert script.read_bytes() == b"new!"
    assert script.stat().st_mtime_ns == MTIME
    if sys.platform != "win32":
        assert script.stat().st_mode & 0o777 == 0o755
    link = target / "venv" / "bin" / "python"
    assert (
        os.path.islink(link)
        and os.readlink(link) == "/usr/bin/python3"
        or ((target / "venv" / "bin" / "python.symlink").read_text().strip() == "A symlink to /usr/bin/python3")
    )
    assert not (tmp_path / "escape.txt").exists()
    assert "Skipped ../escape.txt: not a plain relative path." in said


def test_a_file_is_never_written_through_a_symlink_already_there(http, fake, tmp_path) -> None:
    if sys.platform == "win32":
        pytest.skip("making a symlink needs rights a Windows account may lack")
    _login(http, [])
    target = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir()
    target.mkdir()
    os.symlink(outside, target / "venv")
    said: list[str] = []

    run(["workspaces", "download", PROJECT, "llm", str(target)], http=http, say=said.append)

    assert not (outside / "bin" / "run.sh").exists()
    assert "Skipped venv/bin/run.sh: not a plain relative path." in said


def test_workspaces_rm_asks_first_and_refuses_one_in_use(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["workspaces", "rm", PROJECT, "busy"], http=http, say=said.append) == 1
    assert "End it first" in said[-1]
    assert run(["workspaces", "rm", PROJECT, "llm"], http=http, say=said.append, prompt=lambda _: "n") == 1
    assert fake.deleted == []
    assert run(["workspaces", "rm", PROJECT, WS[:8]], http=http, say=said.append, prompt=lambda _: "y") == 0

    assert fake.deleted == [WS]
    assert said[-1] == "Deleted llm (3 stored files)."


def test_an_unknown_workspace_says_how_to_list_them(http, fake) -> None:
    _login(http, [])

    with pytest.raises(KraterError) as caught:
        run(["workspaces", "download", PROJECT, "nope"], http=http, say=lambda _: None)

    assert "krater workspaces list" in str(caught.value)
