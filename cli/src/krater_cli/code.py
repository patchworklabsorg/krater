"""`--code FOLDER`: run a job from a folder of code, with no image to build.

The folder goes up as a project input under `code/<hash>/` (its content's hash, so a changed file is a new folder and
an unchanged one is never sent twice), the job copies it in like any input, and its command runs inside it on a stock
image. With a `requirements.txt` the command installs it first. `krater run --code` mounts the folder at the same
place in its container, so the local rehearsal runs the same command.

What it leaves out: version control, caches, virtual environments and what Krater's own folders hold (a local
`krater run` writes `checkpoints/` and `outputs/` under `krater-local/`). Data belongs in `krater inputs upload`, so a
folder over `MAX_BYTES` is refused with that advice.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

#: The image a `--code` job runs on when it names none: Krater's form default, PyTorch with CUDA 12.8 (runs on every
#: GPU Krater rents, the RTX 50 series included). Keep in step with `krater.services.compute_jobs.DEFAULT_IMAGE`.
DEFAULT_IMAGE = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime"
#: The folder of the project's inputs that code goes under.
INPUTS_PREFIX = "code"
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".ipynb_checkpoints",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "krater-local",
        "checkpoints",
        "outputs",
        "wandb",
    }
)
SKIP_SUFFIXES = (".pyc", ".pyo")
#: Code, not data: a folder bigger than this is almost always a dataset or weights that belong in inputs.
MAX_BYTES = 100 * 1024 * 1024
MAX_FILES = 5000


class CodeError(Exception):
    pass


@dataclass(frozen=True)
class Code:
    folder: str
    files: list[tuple[str, str]]  # (local path, path inside the folder, with /)
    digest: str
    size: int

    @property
    def input_name(self) -> str:
        """The project input it's stored as: a folder, so it ends in /."""
        return f"{INPUTS_PREFIX}/{self.digest}/"

    @property
    def has_requirements(self) -> bool:
        return any(rel == "requirements.txt" for _, rel in self.files)

    def command(self, command: str) -> str:
        """The job's command: into the folder, the requirements if there are any, then the member's own."""
        parts = [f'cd "$KRATER_INPUT_DIR/{INPUTS_PREFIX}/{self.digest}"']
        if self.has_requirements:
            parts.append("pip install --quiet --no-cache-dir -r requirements.txt")
        return " && ".join(parts) + " && " + command


def collect(folder: str) -> Code:
    """The folder's files, in a stable order, with their hash. Raises `CodeError` when it isn't a usable code folder."""
    if not os.path.isdir(folder):
        raise CodeError(f"--code {folder}: no such folder.")
    files: list[tuple[str, str]] = []
    size = 0
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if name.endswith(SKIP_SUFFIXES):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue  # a broken link, a socket
            files.append((path, os.path.relpath(path, folder).replace(os.sep, "/")))
            size += os.path.getsize(path)
            if len(files) > MAX_FILES or size > MAX_BYTES:
                raise CodeError(
                    f"--code {folder} is more than {MAX_FILES:,} files or {MAX_BYTES // 2**20} MB: that's data, not "
                    "code. Upload data once with `krater inputs upload <project> data/` and give the job --input "
                    "data/, or leave it out of this folder."
                )
    if not files:
        raise CodeError(f"--code {folder} has no files to send.")
    digest = hashlib.sha256()
    for path, rel in files:
        digest.update(rel.encode() + b"\0")
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        digest.update(b"\0")
    return Code(folder=folder, files=files, digest=digest.hexdigest()[:16], size=size)


def describe(code: Code) -> str:
    count = len(code.files)
    return f"{count} file{'s' if count != 1 else ''}, {code.size / 1024:,.0f} KB"
