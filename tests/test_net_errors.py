"""`krater.net_errors`: naming why a request to another service failed."""

from __future__ import annotations

import errno
import socket
import ssl

import httpx

from krater import net_errors
from krater.vast import VastClient, VastUnavailableError
from krater.weave import WeaveUnavailableError


def _wrapped(root: BaseException, outer: type[BaseException] = WeaveUnavailableError) -> BaseException:
    """`outer` raised from an httpx error raised from `root`, as Krater's clients and httpx chain them."""
    request = httpx.Request("GET", "https://example.test/")
    try:
        try:
            try:
                raise root
            except BaseException as exc:
                raise httpx.ConnectError(str(exc), request=request) from exc
        except httpx.ConnectError as exc:
            raise outer("could not reach the service") from exc
    except outer as exc:
        return exc


def test_a_failed_dns_lookup_is_named() -> None:
    exc = _wrapped(socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"))

    text = net_errors.describe(exc)

    assert text.startswith("WeaveUnavailableError: could not reach the service (gaierror: ")
    assert "Temporary failure in name resolution" in text and "DNS lookup failed" in text


def test_each_kind_of_failure_gets_its_own_hint() -> None:
    cases = {
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"): "refused the connection",
        ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"): "cut in the middle",
        OSError(errno.ENETUNREACH, "Network is unreachable"): "no route to the internet",
        OSError(errno.EHOSTUNREACH, "No route to host"): "No route to the host",
        ssl.SSLError("certificate verify failed"): "TLS handshake failed",
    }
    for root, hint in cases.items():
        assert hint in net_errors.describe(_wrapped(root)), root


def test_timeouts_say_whether_it_ever_connected() -> None:
    request = httpx.Request("GET", "https://example.test/")
    assert "while connecting" in net_errors.hint(httpx.ConnectTimeout("timed out", request=request))
    assert "took too long to answer" in net_errors.hint(httpx.ReadTimeout("timed out", request=request))


def test_an_error_that_isnt_the_network_gets_no_hint() -> None:
    assert net_errors.hint(ValueError("bad")) is None
    assert net_errors.describe(ValueError("bad")) == "ValueError: bad"


def test_the_text_is_cut_to_its_limit() -> None:
    assert len(net_errors.describe(ValueError("x" * 5000), limit=300)) == 300


def test_the_vast_clients_unreachable_error_carries_the_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        except socket.gaierror as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc

    client = VastClient("k" * 64, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        client.list_instances()
    except VastUnavailableError as exc:
        text = net_errors.describe(exc)
    else:
        raise AssertionError("expected VastUnavailableError")

    assert "could not reach Vast for GET /api/v1/instances/" in text
    assert "Temporary failure in name resolution" in text and "k" * 64 not in text
