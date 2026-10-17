"""Where a provider's machine is in its life, in words every provider can map to."""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """A rented machine's phase, as the scheduler reads it. Each provider's package maps its own statuses here, so
    the scheduler never reads a provider's status strings.

    A provider maps only what it can tell apart. Vast shows an outbid machine and one that stopped itself the same
    way, so both are `STOPPED` there; the scheduler tells them apart by the runner's silence, not by the phase.
    """

    #: Rented, not running yet (the image pulls). Not billed for the GPU on Vast.
    PROVISIONING = "provisioning"
    #: Its container or VM runs.
    RUNNING = "running"
    #: Not running though the provider still means it to: waiting for its GPU, or taken back and coming back.
    INTERRUPTED = "interrupted"
    #: Stopped, and the provider means it to stay stopped until it's started again.
    STOPPED = "stopped"
    #: The provider still lists it, but it has ended and won't run again.
    GONE = "gone"
