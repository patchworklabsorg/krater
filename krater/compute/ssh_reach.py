"""Whether a box's direct SSH port takes connections, so Krater gives members an address that works.

Vast gives a box two ways in: a direct port on the host's public address (`ports["22/tcp"]`), and its own SSH proxy
(`ssh_host`, `ssh_port`). Krater prefers the direct port: it skips a hop. But a host can list a mapped direct port
that refuses every connection. Seen live 2026-11-22: Vast machine 153597 mapped 22/tcp to port 20056, sshd listened
on 22 in the container and Vast said the box was running, yet the port refused connections from everywhere. The
proxy reached the same sshd. Every box rented there gave its member a dead `krater ssh`.

So the worker opens a TCP connection to a running box's direct port (`krater.compute.scheduler`, `_record_ssh`):

- **Refused** (the host answered: nothing is forwarded) means it doesn't work, and Krater records the proxy. The
  scheduler asks only while the box's runner reports from the current container, and sshd starts before the runner,
  so a refusal then is the host's doing, not a container still starting.
- **Accepted** means it works, and the direct address stays.
- **No answer** (a timeout, an unreachable network, any other error) decides nothing, and the address stays as it
  is: the fault may be on the worker's side.

A host-supplied address that isn't a public one, or a port out of range, can't be what a member connects to, so it
counts as not working without a connection: the worker never connects to a private or loopback address for a host.

Each answer is kept for the worker's process, per instance and address: an accepted one for good, the others for
`RECHECK`, so a box is tested about twice an hour at most, not every pass (each test is a login attempt sshd logs).
A pass tests at most `PROBES_PER_MINUTE` new addresses; the rest wait for the next pass, so hosts that drop packets
can't stretch the scheduler's pass.

Framework-free.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta
from typing import Protocol

#: A refusal answers at once; a dropped connection waits this long and decides nothing.
CONNECT_TIMEOUT_SECONDS = 2.0
#: How long a refusal or no answer is kept before the address is tested again.
RECHECK = timedelta(minutes=30)
#: New tests in one minute (one scheduler pass), so a pass waits at most this many timeouts.
PROBES_PER_MINUTE = 5
#: Answers kept; past this the oldest are dropped (destroyed instances' answers, mostly).
MAX_KNOWN = 1000


def probe_tcp(host: str, port: int) -> bool | None:
    """`True` when a TCP connection to `host:port` opens, `False` when it is refused, `None` with no answer."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            return True
    except ConnectionRefusedError:
        return False
    except Exception:  # noqa: BLE001 - a timeout, no route, a bad address: none of them says the port is closed
        return None


def reachable_address(host: str, port: int) -> bool:
    """Whether `host:port` could be a member's way in: a public IP address and a real port."""
    if not 1 <= port <= 65535:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


class SshCheck(Protocol):
    def direct_works(self, instance_id: str, host: str, port: int, *, now: datetime) -> bool | None:
        """Whether the direct SSH address `host:port` of `instance_id` takes connections; `None` when unknown."""


class LiveSshCheck:
    """The worker's check. One instance lives for the worker's process and remembers each answer."""

    def __init__(self, *, probe=probe_tcp) -> None:
        self._probe = probe
        self._known: dict[tuple[str, str, int], tuple[bool | None, datetime]] = {}
        self._minute: datetime | None = None
        self._probes_this_minute = 0

    def direct_works(self, instance_id: str, host: str, port: int, *, now: datetime) -> bool | None:
        if not reachable_address(host, port):
            return False
        key = (instance_id, host, port)
        known = self._known.get(key)
        if known is not None and (known[0] is True or now - known[1] < RECHECK):
            return known[0]
        minute = now.replace(second=0, microsecond=0)
        if minute != self._minute:
            self._minute, self._probes_this_minute = minute, 0
        if self._probes_this_minute >= PROBES_PER_MINUTE:
            return known[0] if known is not None else None
        self._probes_this_minute += 1
        answer = self._probe(host, port)
        if len(self._known) >= MAX_KNOWN:
            for old in sorted(self._known, key=lambda k: self._known[k][1])[: MAX_KNOWN // 10]:
                del self._known[old]
        self._known[key] = (answer, now)
        return answer
