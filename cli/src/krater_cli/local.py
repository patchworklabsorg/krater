"""`krater run`: a job on this computer, in Docker, set up as a Krater machine sets it up. Free, and it catches the
mistakes that otherwise cost a rental: a typo in the command, a missing package or file, a wrong path.

Krater's machines replace the image's entrypoint with a shell and run the command with `sh -c` in the image's working
directory, with these folders and variables (runner/main.go, krater/compute/launch.py):

- `$KRATER_OUTPUT_DIR` (/krater/outputs) and `$KRATER_CHECKPOINT_DIR` (/krater/checkpoints), saved to Krater; here
  they are folders on this computer, so you can look at what the job wrote;
- `$KRATER_INPUT_DIR` (/krater/inputs), the project's inputs; here, a folder you give (`--input-dir`), read-only;
- `$KRATER_RESUMED` 0 (1 with `--resumed`, to try the resume path with checkpoints already there);
- the project's secrets: their values can't be read back from Krater, so a secret reaches the job here only when it
  is set in this shell under the same name;
- `KRATER_LOCAL=1` here, and `KRATER_SMOKE=1` with `--smoke`, which stops the command after its minutes as a smoke
  test on Krater does.

What a local run can't show: the GPU itself (unless this computer has one and `--local-gpu` is given), the
machine's speed, the network, and the image pull on the host.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

#: Where Krater's machines put the job's folders (runner/main.go's defaults).
OUTPUT_DIR = "/krater/outputs"
CHECKPOINT_DIR = "/krater/checkpoints"
INPUT_DIR = "/krater/inputs"
#: Krater's machines are linux/amd64; asking for it here runs the same image on an ARM Mac (emulated).
PLATFORM = "linux/amd64"
#: How long `docker stop` gives the command after SIGTERM, as Krater's runner does.
STOP_SECONDS = 30
#: docker run's own exit codes: it couldn't run the container, or the command couldn't be found or run.
DOCKER_FAILED, NOT_EXECUTABLE, NOT_FOUND = 125, 126, 127


@dataclass
class LocalRun:
    """One local run of a job body (what `krater submit` would post)."""

    image: str
    command: str
    name: str
    folder: str
    docker: str = "docker"
    input_dir: str | None = None
    gpu: bool = False
    resumed: bool = False
    smoke_minutes: int | None = None
    # Secret names to pass through from this shell's environment (`-e NAME`, so the value never reaches argv).
    env_names: list[str] = field(default_factory=list)

    def container_name(self) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.name).strip("-.")[:40] or "job"
        return f"krater-local-{safe}-{secrets.token_hex(3)}"

    def docker_args(self, container: str) -> list[str]:
        folder = os.path.abspath(self.folder)
        args = [
            self.docker,
            "run",
            "--rm",
            "--name",
            container,
            "--platform",
            PLATFORM,
            "--entrypoint",
            "sh",
            "-e",
            "KRATER_LOCAL=1",
            "-e",
            "KRATER_JOB_ID=local",
            "-e",
            "KRATER_ATTEMPT=1",
            "-e",
            f"KRATER_RESUMED={1 if self.resumed else 0}",
            "-e",
            f"KRATER_OUTPUT_DIR={OUTPUT_DIR}",
            "-e",
            f"KRATER_CHECKPOINT_DIR={CHECKPOINT_DIR}",
            "-e",
            f"KRATER_INPUT_DIR={INPUT_DIR}",
            "-v",
            f"{os.path.join(folder, 'outputs')}:{OUTPUT_DIR}",
            "-v",
            f"{os.path.join(folder, 'checkpoints')}:{CHECKPOINT_DIR}",
        ]
        if self.input_dir:
            args += ["-v", f"{os.path.abspath(self.input_dir)}:{INPUT_DIR}:ro"]
        if self.smoke_minutes:
            args += ["-e", "KRATER_SMOKE=1"]
        for name in self.env_names:
            args += ["-e", name]
        if self.gpu:
            args += ["--gpus", "all"]
        return [*args, self.image, "-c", self.command]


@dataclass
class Outcome:
    code: int
    smoke_passed: bool = False
    interrupted: bool = False


def _quiet(args: Sequence[str]) -> int:
    """`docker stop` prints the container's name; the member doesn't need it."""
    return subprocess.call(args, stdout=subprocess.DEVNULL)


def minutes(count: int) -> str:
    return f"{count} minute" + ("" if count == 1 else "s")


def execute(
    run: LocalRun,
    *,
    popen: Callable[[Sequence[str]], subprocess.Popen] = subprocess.Popen,
    call: Callable[[Sequence[str]], int] = _quiet,
    timer: Callable[[float, Callable[[], None]], threading.Timer] = threading.Timer,
) -> Outcome:
    """Run it in the foreground, its output straight to this terminal. A smoke test's timer, or Ctrl-C, stops the
    container as Krater stops a job: SIGTERM, then SIGKILL after `STOP_SECONDS`."""
    for sub in ("outputs", "checkpoints"):
        os.makedirs(os.path.join(run.folder, sub), exist_ok=True)
    container = run.container_name()
    stopped_by_smoke = threading.Event()

    def stop() -> None:
        call([run.docker, "stop", "-t", str(STOP_SECONDS), container])

    def smoke_up() -> None:
        stopped_by_smoke.set()
        stop()

    process = popen(run.docker_args(container))
    clock = None
    if run.smoke_minutes:
        clock = timer(run.smoke_minutes * 60, smoke_up)
        clock.daemon = True
        clock.start()
    try:
        code = process.wait()
    except KeyboardInterrupt:
        stop()
        process.wait()
        return Outcome(code=130, interrupted=True)
    finally:
        if clock is not None:
            clock.cancel()
    if stopped_by_smoke.is_set():
        return Outcome(code=0, smoke_passed=True)
    return Outcome(code=code)


def files_in(folder: str) -> tuple[int, int]:
    """(files, bytes) under `folder`."""
    count = size = 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            count += 1
    return count, size


def explain(code: int) -> str | None:
    """What an exit code from `docker run` usually means, when it isn't the command's own."""
    return {
        DOCKER_FAILED: "Docker couldn't run the container (see its message above): often the image name or tag is "
        "wrong, or Docker isn't running.",
        NOT_EXECUTABLE: "The command was found but couldn't be run (exit 126): check its permissions.",
        NOT_FOUND: "A command wasn't found in the image (exit 127): check the command, and that the image has it "
        "installed.",
        137: "The command was killed (exit 137): often it ran out of memory. Docker Desktop's memory limit is in its "
        "settings.",
    }.get(code)
