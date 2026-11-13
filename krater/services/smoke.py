"""Smoke tests: a job run for a few minutes to see that it starts and gets going, before paying for the whole run.

A smoke test is the member's own job on the machine they asked for, with `smoke_minutes` set. The runner sets
`KRATER_SMOKE=1` in the command's environment (a script can take fewer steps with it), and stops the command once it
has run `smoke_minutes` without failing. The job then succeeds: what was being tested (the image pulls, the inputs
arrive, the command starts and keeps going on a real GPU) has passed. A command that fails sooner fails the job as
usual, with its exit code and log.

The runner's timer starts with the command, after the image pull and the restores, so a slow pull doesn't eat the
test. The scheduler keeps a backstop: a smoke job's machine is ended at `smoke_minutes + SMOKE_GRACE_MINUTES` of
running time (the job's own max runtime, if shorter), in case the runner never stops it. The job keeps the member's
max runtime, so `krater rerun --no-smoke` queues the full run.
"""

from __future__ import annotations

from typing import Protocol

#: What `krater submit --smoke` asks for when no length is given, in minutes.
DEFAULT_SMOKE_MINUTES = 5
#: The longest smoke test, in minutes: past this it's a short run, not a test.
MAX_SMOKE_MINUTES = 30
#: Running time on top of the test before the scheduler ends the machine itself: the runner copies inputs and restores
#: checkpoints before the command (and its timer) starts, and saves the outputs and log after.
SMOKE_GRACE_MINUTES = 10


class _Shape(Protocol):
    max_runtime_minutes: int
    smoke_minutes: int | None


def runtime_cap_minutes(shape: _Shape) -> int:
    """The running time after which the scheduler ends the job's machine: its max runtime, or for a smoke test the
    test plus `SMOKE_GRACE_MINUTES` when that's shorter. Used for the runtime limit, the budget hold and estimates."""
    smoke = getattr(shape, "smoke_minutes", None)
    if not smoke:
        return shape.max_runtime_minutes
    return min(shape.max_runtime_minutes, smoke + SMOKE_GRACE_MINUTES)


def passed_reason(minutes: int, *, stopped: bool) -> str:
    """The state reason of a smoke test that passed: stopped by its timer, or its command finished first."""
    if stopped:
        return f"Smoke test passed: the command ran {minutes} minute{'s' if minutes != 1 else ''} without failing."
    return "Smoke test passed: the command finished."
