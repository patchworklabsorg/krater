"""Whether Krater's own network is up, before the scheduler acts on a machine that has gone silent.

Krater stops a box, or fails a job and destroys its machine, when the runner on it stops reporting or never starts.
Silence means a broken machine only while runners can reach Krater. When the path they report on is down
(Cloudflare, alastor, the tailnet to weft) or the server has lost its internet connection, every runner looks dead
and none is. Two checks tell the cases apart:

- **Inbound: alastor's ping.** alastor, the public edge, requests `/healthz/edge` every minute over the runners' own
  path (`patchworklabsorg/infra`, `hosts/alastor/edge.nix`); `record_edge_ping` keeps the time. A ping older than
  `EDGE_PING_FRESH` means runners can't get through either. A ping never seen means alastor isn't set up to send it
  (staging, development, or before the infra change is deployed): no hold, as before.
- **Outbound: a connection test.** A TCP connection to a couple of public anycast addresses, with a DNS lookup,
  like `ping 8.8.8.8` but allowed in a container. A test that never passed in this process is a firewall's doing,
  not an outage: no hold.

While either fails, the scheduler holds what it would do about the silence and restarts the machine's deadlines,
so it looks at it afresh, Vast's listing first, once the network is back (`krater.compute.scheduler`). A hold
lasts at most `HOLD_LIMIT` of a runner's silence, then the runner gets one last heartbeat timeout, so a machine
whose runner really died can't bill without end.

Framework-free.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from krater.models import EdgePing

#: alastor pings every minute; a deploy's restart can drop a couple. Older than this, the inbound path is down.
EDGE_PING_FRESH = timedelta(minutes=4)
#: The ping writes its row at most this often, so requests to the open endpoint can't hammer the database.
EDGE_PING_WRITE_EVERY = timedelta(seconds=30)
#: The longest the scheduler holds an action for a runner's silence. Live runners stop their own machines after
#: `runner_self_stop_minutes` (3 h by default) without Krater; this is for one that died.
HOLD_LIMIT = timedelta(hours=6)
#: Where the outbound test connects: Cloudflare's and Google's anycast resolvers, on ports egress allows.
PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("8.8.8.8", 53))
#: A name the outbound test looks up, through the server's own DNS servers.
PROBE_NAME = "one.one.one.one"
PROBE_TIMEOUT_SECONDS = 2.0


def record_edge_ping(session: Session, *, now: datetime) -> bool:
    """Record alastor's ping, at most every `EDGE_PING_WRITE_EVERY`. Returns whether it wrote. The caller commits.

    The endpoint is open: a forged ping can only make the inbound check pass, which is how Krater behaved before it
    had one."""
    row = session.get(EdgePing, 1)
    if row is None:
        session.add(EdgePing(id=1, at=now))
    elif now - row.at >= EDGE_PING_WRITE_EVERY:
        row.at = now
    else:
        return False
    session.flush()
    return True


def last_edge_ping(session: Session) -> datetime | None:
    row = session.get(EdgePing, 1)
    return row.at if row is not None else None


def inbound_problem(session: Session, *, now: datetime) -> str | None:
    """Why runners can't reach Krater, or `None`: never set up, or a fresh ping."""
    last = last_edge_ping(session)
    if last is None or now - last < EDGE_PING_FRESH:
        return None
    return (
        f"alastor's ping hasn't reached Krater since {last:%Y-%m-%d %H:%M} UTC, so runners can't reach it either "
        "(Cloudflare, alastor or the tailnet to weft is down)"
    )


def probe_outbound() -> str | None:
    """Try the outbound connection test: `None` when it passes, else what failed."""
    failures = []
    for host, port in PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
                pass
        except OSError as exc:
            failures.append(f"{host}:{port} ({exc})")
            continue
        try:
            socket.getaddrinfo(PROBE_NAME, 443)
        except OSError as exc:
            return f"connections work but DNS doesn't: looking up {PROBE_NAME} failed ({exc})"
        return None
    return "no connection to " + ", ".join(failures)


class NetworkCheck(Protocol):
    def problem(self, session: Session, *, now: datetime) -> str | None:
        """Why Krater's network is down right now, or `None` when it's up (or can't be told)."""


class LiveNetworkCheck:
    """The worker's check: alastor's ping, then the outbound test. One instance lives for the worker's process, so
    it remembers whether the outbound test has ever passed."""

    def __init__(self, probe=probe_outbound) -> None:
        self._probe = probe
        self._outbound_ever_passed = False

    def problem(self, session: Session, *, now: datetime) -> str | None:
        inbound = inbound_problem(session, now=now)
        if inbound is not None:
            return inbound
        outbound = self._probe()
        if outbound is None:
            self._outbound_ever_passed = True
            return None
        if not self._outbound_ever_passed:
            return None  # blocked here from the start: a firewall, not an outage
        return f"the server's own internet connection is down: {outbound}"
