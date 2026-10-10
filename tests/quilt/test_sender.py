"""`krater.quilt`: the HTTP client and the sender, against a fake Quilt (`httpx.MockTransport`).

Each response class from Quilt's contract, the order per subject, the backoff, the token, and
`SELECT ... FOR UPDATE SKIP LOCKED` with two real connections.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import QuiltOutbox, QuiltOutboxState
from krater.quilt import QuiltClient, QuiltResponse
from krater.quilt import sender as sender_module
from krater.quilt.sender import Outcome, backoff, classify, deliver, next_row
from krater.weave import StubWeaveClient, WeaveUnavailableError
from krater.weave.stub import STUB_QUILT_TOKEN

QUILT_URL = "https://quilt.test"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class FakeQuilt:
    """Answers each event with `answers[event id]` (a status and an optional error), else 201."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, tuple[int, str | None]] = {}
        self.network_error_for: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        event = json.loads(request.content)
        if event["id"] in self.network_error_for:
            raise httpx.ConnectError("no route")
        status, error = self.answers.get(event["id"], (201, None))
        if error is None:
            return httpx.Response(status, json={"status": "applied" if status == 201 else "duplicate"})
        return httpx.Response(status, json={"error": error})

    def sent_ids(self) -> list[str]:
        return [json.loads(r.content)["id"] for r in self.requests]


@pytest.fixture(autouse=True)
def _empty_outbox(request: pytest.FixtureRequest) -> None:
    """Start each `db_session` test from an empty outbox (rolled back with the rest of the test)."""
    if "db_session" in request.fixturenames:
        request.getfixturevalue("db_session").execute(sa.delete(QuiltOutbox))


@pytest.fixture(autouse=True)
def _sender_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alembic's `fileConfig` (run by the `engine` fixture) disables loggers that already exist."""
    monkeypatch.setattr(sender_module.logger, "disabled", False)


@pytest.fixture
def quilt() -> FakeQuilt:
    return FakeQuilt()


@pytest.fixture
def client(quilt: FakeQuilt) -> QuiltClient:
    return QuiltClient(QUILT_URL, http_client=httpx.Client(transport=httpx.MockTransport(quilt.handler)))


@pytest.fixture
def add_row(db_session: Session) -> Callable[..., QuiltOutbox]:
    def _add(external_id: str, type: str = "submission.updated", **fields) -> QuiltOutbox:
        row = QuiltOutbox(
            id=uuid.uuid4(),
            type=type,
            external_id=external_id,
            payload={"external_id": external_id, "status": "approved"},
            occurred_at=NOW - timedelta(minutes=5),
            **fields,
        )
        db_session.add(row)
        db_session.flush()
        return row

    return _add


def _deliver(db_session: Session, client: QuiltClient, weave: StubWeaveClient, *, at: datetime = NOW, limit=50):
    return deliver(db_session, client, weave, limit=limit, now=lambda: at)


# --------------------------------------------------------------------------------------------------
# classify / backoff
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "outcome"),
    [
        (201, None, Outcome.SENT),
        (200, None, Outcome.SENT),
        (409, "unknown_submission", Outcome.RETRY),
        (409, "id_conflict", Outcome.FAILED),
        (409, "submission_exists", Outcome.FAILED),
        (422, "invalid_event", Outcome.FAILED),
        (422, "release_exceeds_commitment", Outcome.FAILED),
        (413, "payload_too_large", Outcome.FAILED),
        (401, "invalid_token", Outcome.RETRY_CONFIG),
        (403, "insufficient_scope", Outcome.RETRY_CONFIG),
        (403, "unknown_client", Outcome.RETRY_CONFIG),
        (404, None, Outcome.RETRY_CONFIG),
        (500, None, Outcome.RETRY),
        (503, "weave_unavailable", Outcome.RETRY),
        (None, "network error", Outcome.RETRY),
    ],
)
def test_classify(status: int | None, error: str | None, outcome: Outcome) -> None:
    assert classify(QuiltResponse(status=status, error=error)) is outcome


def test_backoff_doubles_up_to_an_hour() -> None:
    assert [backoff(n) for n in (1, 2, 3)] == [timedelta(seconds=30), timedelta(seconds=60), timedelta(seconds=120)]
    assert backoff(20) == timedelta(hours=1)


# --------------------------------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------------------------------


def test_the_client_posts_the_event_as_json_with_the_bearer_token(quilt: FakeQuilt, client: QuiltClient) -> None:
    event = {"id": "e1", "type": "spend.recorded", "occurred_at": NOW.isoformat(), "data": {"spent_cents_total": 5}}

    response = client.send_event(event, token="tok")

    assert response.status == 201
    [request] = quilt.requests
    assert str(request.url) == f"{QUILT_URL}/api/v1/events"
    assert request.headers["authorization"] == "Bearer tok"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == event


def test_the_client_reads_quilts_error_key(quilt: FakeQuilt, client: QuiltClient) -> None:
    quilt.answers["e1"] = (409, "id_conflict")

    response = client.send_event({"id": "e1"}, token="tok")

    assert (response.status, response.error) == (409, "id_conflict")


def test_a_network_error_has_no_status(quilt: FakeQuilt, client: QuiltClient) -> None:
    quilt.network_error_for.add("e1")

    response = client.send_event({"id": "e1"}, token="tok")

    assert response.status is None
    assert "ConnectError" in response.error


# --------------------------------------------------------------------------------------------------
# deliver: one row per response class
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [201, 200])
def test_applied_and_duplicate_mark_the_row_sent(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row, status
) -> None:
    row = add_row("sub-a")
    quilt.answers[str(row.id)] = (status, None)

    result = _deliver(db_session, client, weave_stub)

    assert result.sent == 1
    assert (row.state, row.sent_at, row.attempts, row.last_status) == (QuiltOutboxState.SENT, NOW, 1, status)
    [request] = quilt.requests
    assert request.headers["authorization"] == f"Bearer {STUB_QUILT_TOKEN}"
    assert json.loads(request.content) == row.event_body()


@pytest.mark.parametrize(
    ("status", "error"), [(409, "unknown_submission"), (503, "weave_unavailable"), (500, None), (None, None)]
)
def test_retryable_answers_keep_the_row_pending_with_backoff(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row, status, error
) -> None:
    row = add_row("sub-a")
    if status is None:
        quilt.network_error_for.add(str(row.id))
    else:
        quilt.answers[str(row.id)] = (status, error)

    result = _deliver(db_session, client, weave_stub)

    assert result.retried == 1
    assert row.state is QuiltOutboxState.PENDING
    assert row.attempts == 1
    assert row.last_status == status
    assert row.next_attempt_at == NOW + timedelta(seconds=30)

    # Not due yet: nothing is sent.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=10))
    assert len(quilt.requests) == 1

    # Due again: the same event goes again, and the next wait is longer.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=31))
    assert quilt.sent_ids() == [str(row.id), str(row.id)]
    assert row.attempts == 2
    assert row.next_attempt_at == NOW + timedelta(seconds=31) + timedelta(seconds=60)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (409, "id_conflict"),
        (409, "submission_exists"),
        (422, "invalid_event"),
        (422, "release_exceeds_commitment"),
        (413, "payload_too_large"),
    ],
)
def test_permanent_refusals_mark_the_row_failed(
    db_session: Session,
    quilt: FakeQuilt,
    client: QuiltClient,
    weave_stub: StubWeaveClient,
    add_row,
    status,
    error,
    caplog,
) -> None:
    row = add_row("sub-a")
    quilt.answers[str(row.id)] = (status, error)

    result = _deliver(db_session, client, weave_stub)

    assert result.failed == 1
    assert (row.state, row.last_status, row.last_error) == (QuiltOutboxState.FAILED, status, error)
    assert row.next_attempt_at is None
    assert "an admin must look" in caplog.text

    _deliver(db_session, client, weave_stub, at=NOW + timedelta(days=1))
    assert len(quilt.requests) == 1


def test_a_401_retries_later_drops_the_token_and_stops_the_run(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row, caplog
) -> None:
    first = add_row("sub-a")
    add_row("sub-b")
    quilt.answers[str(first.id)] = (401, "invalid_token")

    result = _deliver(db_session, client, weave_stub)

    assert result.stopped == "http_401"
    assert quilt.sent_ids() == [str(first.id)]
    assert first.state is QuiltOutboxState.PENDING
    assert first.next_attempt_at == NOW + timedelta(seconds=30)
    assert weave_stub.quilt_token_invalidations == 1
    assert "check KRATER_QUILT_URL" in caplog.text


def test_a_403_retries_later_and_keeps_the_token(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row("sub-a")
    quilt.answers[str(row.id)] = (403, "unknown_client")

    result = _deliver(db_session, client, weave_stub)

    assert result.stopped == "http_403"
    assert row.state is QuiltOutboxState.PENDING
    assert weave_stub.quilt_token_invalidations == 0


def test_no_token_touches_no_row(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row, monkeypatch
) -> None:
    row = add_row("sub-a")

    def refuse() -> str:
        raise WeaveUnavailableError("down")

    monkeypatch.setattr(weave_stub, "quilt_token", refuse)

    result = _deliver(db_session, client, weave_stub)

    assert result.stopped == "no_token"
    assert quilt.requests == []
    assert (row.state, row.attempts) == (QuiltOutboxState.PENDING, 0)


def test_the_token_is_fetched_once_per_run(
    db_session: Session, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    add_row("sub-a")
    add_row("sub-b")

    _deliver(db_session, client, weave_stub)

    assert weave_stub.quilt_token_requests == 1


def test_a_run_sends_at_most_limit_rows(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    for n in range(3):
        add_row(f"sub-{n}")

    assert _deliver(db_session, client, weave_stub, limit=2).sent == 2
    assert len(quilt.requests) == 2


# --------------------------------------------------------------------------------------------------
# Order per subject
# --------------------------------------------------------------------------------------------------


def test_rows_go_oldest_first(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    rows = [add_row("sub-a"), add_row("sub-b"), add_row("sub-a")]

    _deliver(db_session, client, weave_stub)

    assert quilt.sent_ids() == [str(row.id) for row in rows]


def test_a_failing_row_holds_back_the_later_rows_of_its_subject_only(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    a1 = add_row("sub-a", type="submission.created")
    a2 = add_row("sub-a", type="budget.committed")
    b1 = add_row("sub-b")
    quilt.answers[str(a1.id)] = (503, "weave_unavailable")

    _deliver(db_session, client, weave_stub)

    assert quilt.sent_ids() == [str(a1.id), str(b1.id)]
    assert a2.state is QuiltOutboxState.PENDING
    assert a2.attempts == 0

    # Before a1 is due again, a2 still waits behind it.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=5))
    assert quilt.sent_ids() == [str(a1.id), str(b1.id)]

    del quilt.answers[str(a1.id)]
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1))
    assert quilt.sent_ids()[-2:] == [str(a1.id), str(a2.id)]
    assert {a1.state, a2.state} == {QuiltOutboxState.SENT}


def test_a_failed_row_blocks_its_subject_until_an_admin_acts(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    add_row("sub-a", state=QuiltOutboxState.FAILED)
    blocked = add_row("sub-a")

    _deliver(db_session, client, weave_stub)
    assert quilt.requests == []

    blocked_before = add_row("sub-b", state=QuiltOutboxState.SKIPPED)
    free = add_row("sub-b")
    _deliver(db_session, client, weave_stub)
    assert quilt.sent_ids() == [str(free.id)]
    assert blocked.state is QuiltOutboxState.PENDING
    assert blocked_before.state is QuiltOutboxState.SKIPPED


def test_sent_and_skipped_rows_are_never_sent_again(
    db_session: Session, quilt: FakeQuilt, client: QuiltClient, weave_stub: StubWeaveClient, add_row
) -> None:
    add_row("sub-a", state=QuiltOutboxState.SENT)
    add_row("sub-a", state=QuiltOutboxState.SKIPPED)

    _deliver(db_session, client, weave_stub)

    assert quilt.requests == []


# --------------------------------------------------------------------------------------------------
# SKIP LOCKED: two workers, two real connections
# --------------------------------------------------------------------------------------------------


def test_two_workers_never_take_the_same_row_or_skip_ahead_in_a_subject(engine: Engine) -> None:
    tag = uuid.uuid4().hex
    subject_a, subject_b = f"lock-a-{tag}", f"lock-b-{tag}"
    ids = []
    with Session(engine) as setup:
        for subject in (subject_a, subject_a, subject_b):
            row = QuiltOutbox(
                id=uuid.uuid4(), type="submission.updated", external_id=subject, payload={}, occurred_at=NOW
            )
            setup.add(row)
            setup.flush()
            ids.append(row.id)
        setup.commit()
    a1, a2, b1 = ids

    worker_one = Session(engine)
    worker_two = Session(engine)
    try:
        taken_by_one = next_row(worker_one, NOW)
        assert taken_by_one is not None
        first_id = taken_by_one.id

        # a1 is locked by worker one: worker two skips it, and a2 must wait behind it, so b1 is next.
        taken_by_two = next_row(worker_two, NOW)
        assert taken_by_two is not None
        second_id = taken_by_two.id
    finally:
        worker_one.rollback()
        worker_two.rollback()
        worker_one.close()
        worker_two.close()
        with Session(engine) as cleanup:
            cleanup.execute(sa.delete(QuiltOutbox).where(QuiltOutbox.external_id.in_([subject_a, subject_b])))
            cleanup.commit()
    assert (first_id, second_id) == (a1, b1)
    assert a2 not in (first_id, second_id)


# --------------------------------------------------------------------------------------------------
# run: the worker's entry point
# --------------------------------------------------------------------------------------------------


def test_run_does_nothing_without_a_quilt_url(
    db_session: Session, weave_stub: StubWeaveClient, add_row, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="krater.quilt.sender")
    monkeypatch.setattr(sender_module, "_disabled_logged", False)
    row = add_row("sub-a")

    assert sender_module.run(db_session, weave_stub, Settings(quilt_url="")) is None
    assert sender_module.run(db_session, weave_stub, Settings(quilt_url="")) is None

    assert row.state is QuiltOutboxState.PENDING
    assert weave_stub.quilt_token_requests == 0
    assert caplog.text.count("KRATER_QUILT_URL is blank") == 1


def test_run_sends_to_the_configured_url(
    db_session: Session, quilt: FakeQuilt, weave_stub: StubWeaveClient, add_row, monkeypatch
) -> None:
    row = add_row("sub-a")
    real_client = QuiltClient

    def fake_client(base_url: str, **kwargs) -> QuiltClient:
        return real_client(base_url, http_client=httpx.Client(transport=httpx.MockTransport(quilt.handler)))

    monkeypatch.setattr(sender_module, "QuiltClient", fake_client)

    result = sender_module.run(db_session, weave_stub, Settings(quilt_url=QUILT_URL + "/"))

    assert result.sent >= 1
    assert row.state is QuiltOutboxState.SENT
    assert str(quilt.requests[0].url) == f"{QUILT_URL}/api/v1/events"
