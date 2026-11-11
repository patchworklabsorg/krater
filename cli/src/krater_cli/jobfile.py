"""Job files (`krater submit -f job.toml`) and sweeps (`--count`, `--sweep`), both on the member's side.

A job file holds `krater submit`'s options under their long names (`max-runtime = 600`, or `max_runtime`), plus
`project` and a `[sweep]` table. An option given on the command line wins over the file.

A sweep queues one job per combination of its values (times `--count`). Krater's jobs have no environment of their
own, so each job's command starts with `export KRATER_SWEEP_INDEX=<n> <name>=<value> ...;`: the job page shows
exactly what it ran with. Each job's name gets `-<n>`.
"""

from __future__ import annotations

import argparse
import itertools
import re
import shlex
import sys
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

#: The most jobs one `krater submit` queues: a typo in a sweep shouldn't spend a project's budget.
MAX_SWEEP_JOBS = 100
INDEX_VAR = "KRATER_SWEEP_INDEX"
_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
#: Options that collect a list; a file gives them as a TOML list (or one value).
_LISTS = frozenset({"gpu", "input"})
#: Not options of a job: what the file can't set.
_NOT_IN_FILE = frozenset({"file", "help", "json", "estimate", "confirm_transfer_cost"})


class JobFileError(Exception):
    """A job file or sweep the CLI can't use; the message says why."""


def load(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise JobFileError(f"Can't read {path}: {exc.strerror or exc}.") from None
    except tomllib.TOMLDecodeError as exc:
        raise JobFileError(f"{path} isn't valid TOML: {exc}") from None


def _options(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    """The submit parser's actions by file key (`max_runtime`), positional `project` included."""
    found: dict[str, argparse.Action] = {}
    for action in parser._actions:
        if not action.option_strings:
            found[action.dest] = action
        for option in action.option_strings:
            if option.startswith("--"):
                found[option[2:].replace("-", "_")] = action
    return found


def _given(value: Any) -> bool:
    return value is not None and value is not False and value != []


def apply(data: dict[str, Any], args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Set every option the command line left out from the file. Returns the file's `[sweep]` table."""
    options = _options(parser)
    sweep = data.get("sweep", {})
    if not isinstance(sweep, dict):
        raise JobFileError("[sweep] must be a table: name = [values].")
    for raw_key, value in data.items():
        key = raw_key.replace("-", "_")
        if key == "sweep":
            continue
        action = options.get(key)
        if action is None or key in _NOT_IN_FILE:
            raise JobFileError(f"The job file has '{raw_key}', which isn't an option of krater submit.")
        if _given(getattr(args, action.dest)):
            continue  # the command line wins
        setattr(args, action.dest, _convert(raw_key, key, action, value))
    return sweep


def _convert(raw_key: str, key: str, action: argparse.Action, value: Any) -> Any:
    if action.nargs == 0:  # a flag
        if not isinstance(value, bool):
            raise JobFileError(f"{raw_key} must be true or false.")
        return value
    if key in _LISTS:
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(v, str) for v in values):
            raise JobFileError(f"{raw_key} must be a name or a list of names.")
        return values
    if isinstance(value, (dict, list, bool)):
        raise JobFileError(f"{raw_key} must be a single value.")
    if action.type is not None:
        try:
            value = action.type(str(value))
        except (argparse.ArgumentTypeError, ValueError) as exc:
            reason = "must be a whole number" if action.type is int else str(exc)
            raise JobFileError(f"{raw_key}: {reason}.".replace("..", ".")) from None
    else:
        value = str(value)
    if action.choices is not None and value not in action.choices:
        raise JobFileError(f"{raw_key} must be one of: {', '.join(action.choices)}.")
    return value


def sweep_values(cli: list[str], table: dict[str, Any]) -> dict[str, list[str]]:
    """The values to sweep, by variable name: the file's `[sweep]`, then each `--sweep name=a,b,c` (which wins)."""
    values: dict[str, list[str]] = {}
    for name, raw in table.items():
        items = raw if isinstance(raw, list) else [raw]
        if not items or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in items):
            raise JobFileError(f"[sweep] {name} must be a list of values.")
        values[_var(name)] = [str(v) for v in items]
    for item in cli:
        name, sep, raw = item.partition("=")
        items = [v.strip() for v in raw.split(",") if v.strip()]
        if not sep or not items:
            raise JobFileError(f"--sweep {item}: give it as name=value1,value2.")
        values[_var(name.strip())] = items
    return values


def _var(name: str) -> str:
    if not _VAR.match(name) or name.upper().startswith("KRATER_") or name in ("PATH", "HOME"):
        raise JobFileError(
            f"Can't sweep '{name}': use a name of letters, digits and _ that isn't PATH, HOME or KRATER_..."
        )
    return name


def expand(body: dict[str, Any], values: dict[str, list[str]], count: int) -> list[tuple[dict[str, Any], dict]]:
    """One (body, variables) per job to queue. Without a sweep, the body itself."""
    if count < 1:
        raise JobFileError("--count must be 1 or more.")
    if not values and count == 1:
        return [(body, {})]
    if body.get("kind") == "box":
        raise JobFileError("A sweep is for jobs, not boxes.")
    if not body.get("command"):
        raise JobFileError("A sweep needs --command: its values reach the command as variables.")
    combos = list(itertools.product(*values.values())) if values else [()]
    total = len(combos) * count
    if total > MAX_SWEEP_JOBS:
        raise JobFileError(f"That sweep is {total} jobs; one submit queues at most {MAX_SWEEP_JOBS}.")
    runs = []
    for index, (combo, _) in enumerate(itertools.product(combos, range(count))):
        variables = {INDEX_VAR: str(index), **dict(zip(values, combo))}
        exports = " ".join(f"{name}={shlex.quote(value)}" for name, value in variables.items())
        runs.append(
            (
                {**body, "name": f"{body['name']}-{index}", "command": f"export {exports}; {body['command']}"},
                variables,
            )
        )
    return runs
