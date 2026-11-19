"""Saying why a request to another service failed, for admin alerts and logs.

Krater's clients wrap a network failure in their own error (`VastUnavailableError`, `WeaveUnavailableError` ...), and
the wrapped error says only "could not reach Vast". What went wrong sits further down the exception chain: the
socket's own error. A DNS lookup that failed, a refused connection and a connect that timed out have different
causes (this server's DNS servers, the service, the network between them), so `describe` names it.

Only exception types and their messages are read, never a request or response: a URL's query or an
Authorization header can't end up in the text. Framework-free.
"""

from __future__ import annotations

import errno
import socket
import ssl

import httpx

#: How far down the `__cause__` / `__context__` chain to look.
_MAX_DEPTH = 8

#: The `hint`s that mean this server's own network or DNS is down rather than one service: when several alerts open
#: with one of these at once, `krater.services.ops_alerts.deliver` posts them as one.
DNS_FAILED = (
    "The DNS lookup failed: this server's DNS servers didn't answer, or its own internet connection is "
    "down. The service itself may be fine."
)
NO_ROUTE = "This server has no route to the internet (network unreachable): its own connection is down."
OWN_NETWORK_DOWN = (DNS_FAILED, NO_ROUTE)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < _MAX_DEPTH and all(current is not e for e in seen):
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _eai(name: str) -> int | None:
    return getattr(socket, name, None)


def hint(exc: BaseException) -> str | None:
    """A plain-English cause of a network failure anywhere in `exc`'s chain, or `None` when it wasn't one."""
    chain = _chain(exc)
    for link in reversed(chain):  # the root first: it is the most specific
        if isinstance(link, socket.gaierror):
            if link.errno is not None and link.errno == _eai("EAI_NONAME"):
                return "DNS says the host name doesn't exist (or this server's DNS servers can't find it)."
            return DNS_FAILED
        if isinstance(link, ConnectionRefusedError):
            return "The host refused the connection: it is up, but nothing answered on that port."
        if isinstance(link, ConnectionResetError | ConnectionAbortedError | BrokenPipeError):
            return "The connection was cut in the middle of the request."
        if isinstance(link, ssl.SSLError):
            return "The TLS handshake failed: a certificate problem, or something in between answering instead."
        if isinstance(link, OSError) and link.errno in (errno.ENETUNREACH, errno.ENETDOWN):
            return NO_ROUTE
        if isinstance(link, OSError) and link.errno == errno.EHOSTUNREACH:
            return "No route to the host: the network between this server and the service is down."
    for link in chain:
        if isinstance(link, httpx.ConnectTimeout):
            return (
                "Nothing answered while connecting: the network between this server and the service is down or "
                "dropping packets, or the service is down."
            )
        if isinstance(link, httpx.ReadTimeout | httpx.WriteTimeout):
            return "Connected, but the service took too long to answer: it is slow or overloaded."
        if isinstance(link, httpx.PoolTimeout):
            return "Every connection in this process's pool was busy: requests are piling up."
        if isinstance(link, httpx.RemoteProtocolError):
            return "The service closed the connection without a complete answer."
    return None


def describe(exc: BaseException, *, limit: int = 1000) -> str:
    """`Type: message` for `exc`, then the root cause's own text when the message leaves it out ("could not reach
    Vast ...: ConnectError" says nothing of DNS), then `hint`."""
    text = f"{type(exc).__name__}: {exc}"
    chain = _chain(exc)
    root = chain[-1]
    root_text = str(root)
    if root is not exc and root_text and root_text not in text:
        text += f" ({type(root).__name__}: {root_text})"
    cause = hint(exc)
    if cause:
        text += f" {cause}"
    return text[:limit]
