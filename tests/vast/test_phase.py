"""Vast's instance statuses mapped to the neutral phase the scheduler reads."""

from __future__ import annotations

import pytest

from krater.providers import Phase
from krater.vast import Instance


def _seen(actual, intended, cur="running"):
    return Instance.from_json({"id": 1, "actual_status": actual, "intended_status": intended, "cur_state": cur})


@pytest.mark.parametrize(
    ("actual", "intended", "cur", "phase", "stopping"),
    [
        (None, "running", "running", Phase.PROVISIONING, False),  # the image pulls
        ("loading", "running", "running", Phase.PROVISIONING, False),
        ("running", "running", "running", Phase.RUNNING, False),
        # Right after Krater's stop (staging, 2026-10-11): still running, no longer meant to.
        ("running", "stopped", "stopped", Phase.RUNNING, True),
        # Krater's own stop, an outbid machine and a runner's self-stop all look like this (staging, 2026-10-14).
        ("exited", "stopped", "stopped", Phase.STOPPED, True),
        ("stopped", "stopped", "stopped", Phase.STOPPED, True),
        (None, "stopped", "stopped", Phase.STOPPED, True),
        # Started again but its GPU is taken: Vast keeps trying.
        ("exited", "running", "stopped", Phase.INTERRUPTED, False),
        ("offline", "running", "running", Phase.INTERRUPTED, False),
        # A member started their own stopped instance with the container's key.
        ("running", "running", "stopped", Phase.RUNNING, False),
    ],
)
def test_vast_statuses_map_to_a_phase(actual, intended, cur, phase, stopping) -> None:
    seen = _seen(actual, intended, cur)
    assert seen.phase is phase
    assert seen.stopping is stopping
    assert seen.status == actual
