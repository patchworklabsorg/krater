"""`krater init`: write the starter job (the repo's `examples/starter`) into a folder, ready to run.

The files ship inside this package (`starter/`), because `krater` is installed on its own from `cli/`.
`tests/test_init.py` checks they match `examples/starter`. Dot files are stored without the dot (`gitignore`), and
the workflow flat (`publish.yml`), so packaging tools don't treat them as their own; `FILES` names where each goes.
"""

from __future__ import annotations

import os
import re
from importlib import resources

#: (name in the package's starter/, where it goes in the new folder)
FILES = (
    ("train.py", "train.py"),
    ("job.toml", "job.toml"),
    ("README.md", "README.md"),
    ("Dockerfile", "Dockerfile"),
    ("gitignore", ".gitignore"),
    ("dockerignore", ".dockerignore"),
    ("publish.yml", ".github/workflows/publish.yml"),
)
_PROJECT_LINE = re.compile(r'^project = "your-project"', re.MULTILINE)


class InitError(Exception):
    pass


def read(name: str) -> str:
    return resources.files("krater_cli").joinpath("starter", name).read_text(encoding="utf-8")


def write(folder: str, *, project: str | None = None, force: bool = False) -> list[str]:
    """Write the starter into `folder` (made if needed). Refuses to overwrite a file unless `force`. Returns the paths
    written, relative to `folder`."""
    clashes = [target for _, target in FILES if os.path.exists(os.path.join(folder, target))]
    if clashes and not force:
        raise InitError(
            f"{folder} already has {', '.join(clashes)}. Choose a new folder (krater init my-job), or --force to "
            "replace them."
        )
    written = []
    for name, target in FILES:
        text = read(name)
        if name == "job.toml" and project:
            text = _PROJECT_LINE.sub(f"project = {_toml_string(project)}", text, count=1)
        path = os.path.join(folder, *target.split("/"))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        written.append(target)
    return written


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
