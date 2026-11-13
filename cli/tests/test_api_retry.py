"""`Krater.request` waits out a deploy's 502s for requests safe to send twice, and never repeats a change."""

from __future__ import annotations

import httpx
import pytest

from krater_cli import api
from krater_cli.api import Krater, KraterError


@pytest.fixture(autouse=True)
def _signed_in(monkeypatch) -> None:
    monkeypatch.setattr(api.auth, "current", lambda http: {"url": "https://krater.test", "access_token": "t"})


def _krater(answers: list) -> tuple[Krater, list[str], list[float]]:
    """A `Krater` whose server gives `answers` in turn: a status code, or an exception to raise."""
    seen: list[str] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"ok": True} if answer == 200 else {"error": "bad gateway"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Krater(http, sleep=slept.append), seen, slept


def test_a_read_waits_out_a_deploy(capsys) -> None:
    krater, seen, slept = _krater([502, 503, 200])

    assert krater.get("/jobs/abc") == {"ok": True}

    assert seen == ["GET /api/v1/jobs/abc"] * 3
    assert slept == [1.0, 2.0]
    assert "trying again" in capsys.readouterr().err


def test_an_estimate_is_safe_to_repeat() -> None:
    krater, seen, _ = _krater([502, 200])

    assert krater.post("/projects/p/jobs/estimate", json={}) == {"ok": True}
    assert len(seen) == 2


def test_a_submit_is_never_sent_twice_after_a_502() -> None:
    """Krater may have queued the job before the proxy gave up: sending it again could queue two."""
    krater, seen, slept = _krater([502, 200])

    with pytest.raises(KraterError) as raised:
        krater.post("/projects/p/jobs", json={})

    assert raised.value.status == 502
    assert seen == ["POST /api/v1/projects/p/jobs"] and slept == []


def test_no_connection_is_tried_again_whatever_the_method() -> None:
    """Nothing reached Krater, so even a submit is safe to send again."""
    krater, seen, _ = _krater([httpx.ConnectError("refused"), 200])

    assert krater.post("/projects/p/jobs", json={}) == {"ok": True}
    assert len(seen) == 2


def test_it_gives_up_after_about_15_seconds() -> None:
    krater, seen, slept = _krater([502] * 5)

    with pytest.raises(KraterError):
        krater.get("/me")

    assert len(seen) == 5 and slept == [1.0, 2.0, 4.0, 8.0]


def test_a_real_answer_is_not_retried() -> None:
    krater, seen, _ = _krater([404])

    with pytest.raises(KraterError):
        krater.get("/jobs/nope")
    assert len(seen) == 1
