"""Whether a box's direct SSH port takes connections, so Krater gives members an address that works.

Vast gives a box two ways in: a direct port on the host's public address (`ports["22/tcp"]`), and its own SSH proxy
(`ssh_host`, `ssh_port`). Krater prefers the direct port: it skips a hop. But a host can list a mapped direct port
that refuses every connection. Seen live 2026-11-22: Vast machine 153597 mapped 22/tcp to port 20056 and Vast said
the box was running, yet the port refused connections from everywhere, while the proxy reached the same container,
sshd and keys. Every box rented there gave its member a dead `krater ssh`.

So once the box's runner has started (Vast's init starts sshd before the startup script that starts the runner),
the worker opens one TCP connection to the direct port. When it fails and the provider gave a proxy, Krater records
the proxy. The answer is kept for the worker's process, per instance and address, so a box is tested once, not on
every pass: each test is a login attempt sshd logs.

Framework-free.
"""

from __future__ import annotations

import socket
from typing import Protocol

#: A refused connection answers at once; a dropped one waits this long.
CONNECT_TIMEOUT_SECONDS = 4.0


def probe_tcp(host: str, port: int) -> bool:
    """Whether a TCP connection to `host:port` opens."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


class SshCheck(Protocol):
    def direct_works(self, instance_id: str, host: str, port: int) -> bool:
        """Whether the direct SSH address `host:port` of `instance_id` takes connections."""


class LiveSshCheck:
    """The worker's check. One instance lives for the worker's process and remembers each answer."""

    def __init__(self, *, probe=probe_tcp) -> None:
        self._probe = probe
        self._known: dict[tuple[str, str, int], bool] = {}

    def direct_works(self, instance_id: str, host: str, port: int) -> bool:
        key = (instance_id, host, port)
        if key not in self._known:
            if len(self._known) > 1000:
                self._known.clear()  # destroyed instances' answers; the live ones are tested again once
            self._known[key] = self._probe(host, port)
        return self._known[key]
