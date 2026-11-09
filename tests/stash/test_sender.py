"""`krater.stash`: the HTTP client and the sender, against a fake Stash (`httpx.MockTransport`).

Each response class from Stash's contract (`docs/api.md`): 201 new, 200 the same submission and URL again,
422 an invalid URL, 503 Weave down, 401/403 the token; the backoff, the token, the status refresh, and the
request body Krater sends.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import Project, StashArchive, StashArchiveState
from krater.services import stash_archives
from krater.services.actor import Actor
from krater.stash import StashClient, StashResponse
from krater.stash import sender as sender_module
from krater.stash.sender import Outcome, archive_request, backoff, classify, deliver
from krater.weave import StubWeaveClient, WeaveUnavailableError
from krater.weave.stub import STUB_STASH_TOKEN
from tests.factories import new_project

STASH_URL = "https://stash.test"
BASE_URL = "https://krater.test"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
REPO = "https://github.com/someone/project"


def archive_body(archive_id: str = "arc_1", *, status: str = "queued", completed_at: str | None = None) -> dict:
    return {
        "id": archive_id,
        "status": status,
        "url": REPO,
        "page_url": f"{STASH_URL}/archives/{archive_id.removeprefix('arc_')}",
        "completed_at": completed_at,
        "captures": [],
    }


class FakeStash:
    """Answers each `POST` with `answers[url]` (a status and an optional error), else 201 with a new archive,
    and each `GET` with `archives[archive id]`, else 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, tuple[int, str | None]] = {}
        self.archives: dict[str, dict] = {}
        self.network_error_for: set[str] = set()
        self.created = 0
        self.retry_after: str | None = None  # the `Retry-After` header on every 429
        self.get_status: int | None = None  # answer every `GET` with this status instead

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            archive_id = request.url.path.rsplit("/", 1)[-1]
            if archive_id in self.network_error_for:
                raise httpx.ConnectError("no route")
            if self.get_status is not None:
                headers = self._headers(self.get_status)
                return httpx.Response(self.get_status, json={"error": "rate_limited"}, headers=headers)
            archive = self.archives.get(archive_id)
            if archive is None:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json=archive)
        body = json.loads(request.content)
        if body["url"] in self.network_error_for:
            raise httpx.ConnectError("no route")
        status, error = self.answers.get(body["url"], (201, None))
        if error is not None:
            return httpx.Response(status, json={"error": error}, headers=self._headers(status))
        self.created += 1
        archive = archive_body(f"arc_{self.created}")
        self.archives[archive["id"]] = archive
        return httpx.Response(status, json=archive)

    def _headers(self, status: int) -> dict[str, str]:
        return {"Retry-After": self.retry_after} if status == 429 and self.retry_after is not None else {}

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


@pytest.fixture(autouse=True)
def _sender_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alembic's `fileConfig` (run by the `engine` fixture) disables loggers that already exist."""
    monkeypatch.setattr(sender_module.logger, "disabled", False)


@pytest.fixture
def stash() -> FakeStash:
    return FakeStash()


@pytest.fixture
def client(stash: FakeStash) -> StashClient:
    return StashClient(STASH_URL, http_client=httpx.Client(transport=httpx.MockTransport(stash.handler)))


@pytest.fixture
def add_row(db_session: Session, member: Actor) -> Callable[..., StashArchive]:
    """A pending row for a fresh submitted project with `url` as its repository link."""

    def _add(url: str = REPO, **fields) -> StashArchive:
        project = new_project(db_session, member, title="Rover", write_up="A rover.", repo_url=url)
        project.current_revision.submitted_at = NOW - timedelta(minutes=5)
        db_session.flush()
        row = StashArchive(
            project_id=project.id, revision_id=project.current_revision_id, url=url, source="repo_url", **fields
        )
        db_session.add(row)
        db_session.flush()
        return row

    return _add


def _deliver(db_session: Session, client: StashClient, weave: StubWeaveClient, *, at: datetime = NOW, limit=50):
    return deliver(db_session, client, weave, limit=limit, base_url=BASE_URL, now=lambda: at)


# --------------------------------------------------------------------------------------------------
# classify / backoff
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "archive", "outcome"),
    [
        (201, None, archive_body(), Outcome.ARCHIVED),
        (200, None, archive_body(), Outcome.ARCHIVED),
        (200, None, None, Outcome.RETRY),
        (422, "invalid", None, Outcome.FAILED),
        (413, "too_large", None, Outcome.FAILED),
        (401, "invalid_token", None, Outcome.RETRY_CONFIG),
        (403, "insufficient_scope", None, Outcome.RETRY_CONFIG),
        (404, None, None, Outcome.RETRY_CONFIG),
        (429, "rate_limited", None, Outcome.RATE_LIMITED),
        (500, None, None, Outcome.RETRY),
        (503, "weave_unavailable", None, Outcome.RETRY),
        (None, "network error", None, Outcome.RETRY),
    ],
)
def test_classify(status: int | None, error: str | None, archive: dict | None, outcome: Outcome) -> None:
    assert classify(StashResponse(status=status, error=error, archive=archive)) is outcome


def test_backoff_doubles_up_to_an_hour() -> None:
    assert [backoff(n) for n in (1, 2, 3)] == [timedelta(seconds=30), timedelta(seconds=60), timedelta(seconds=120)]
    assert backoff(20) == timedelta(hours=1)


# --------------------------------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------------------------------


def test_the_client_posts_the_url_as_json_with_the_bearer_token(stash: FakeStash, client: StashClient) -> None:
    reference = {"patch": "ganymede", "submission": "sub-1"}

    response = client.create_archive(url=REPO, note="A note.", reference=reference, token="tok")

    assert response.status == 201
    assert response.archive is not None and response.archive["id"] == "arc_1"
    [request] = stash.requests
    assert str(request.url) == f"{STASH_URL}/api/v1/archives"
    assert request.headers["authorization"] == "Bearer tok"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"url": REPO, "note": "A note.", "reference": reference}


def test_a_200_is_the_earlier_archive(stash: FakeStash, client: StashClient) -> None:
    stash.answers[REPO] = (200, None)

    response = client.create_archive(url=REPO, note="", reference={}, token="tok")

    assert (response.status, response.error) == (200, None)
    assert response.archive is not None and response.archive["page_url"].endswith("/archives/1")


def test_the_client_reads_stashs_error_key(stash: FakeStash, client: StashClient) -> None:
    stash.answers[REPO] = (422, "invalid")

    response = client.create_archive(url=REPO, note="", reference={}, token="tok")

    assert (response.status, response.error, response.archive) == (422, "invalid", None)


def test_a_503_from_stash_has_its_error(stash: FakeStash, client: StashClient) -> None:
    stash.answers[REPO] = (503, "weave_unavailable")

    response = client.create_archive(url=REPO, note="", reference={}, token="tok")

    assert (response.status, response.error) == (503, "weave_unavailable")


def test_a_network_error_has_no_status(stash: FakeStash, client: StashClient) -> None:
    stash.network_error_for.add(REPO)

    response = client.create_archive(url=REPO, note="", reference={}, token="tok")

    assert response.status is None
    assert "ConnectError" in response.error


def test_the_client_reads_one_archive(stash: FakeStash, client: StashClient) -> None:
    stash.archives["arc_9"] = archive_body("arc_9", status="done", completed_at="2026-11-08T12:01:00Z")

    response = client.get_archive("arc_9", token="tok")

    assert response.status == 200
    assert response.archive is not None and response.archive["status"] == "done"
    [request] = stash.requests
    assert str(request.url) == f"{STASH_URL}/api/v1/archives/arc_9"
    assert request.headers["authorization"] == "Bearer tok"
    assert client.get_archive("arc_missing", token="tok").status == 404


# --------------------------------------------------------------------------------------------------
# The request body
# --------------------------------------------------------------------------------------------------


def test_the_request_names_the_project_and_the_submitter_by_patchwork_id(db_session: Session, add_row, member) -> None:
    row = add_row()
    project = db_session.get(Project, row.project_id)
    assert project is not None

    body = archive_request(row, base_url=BASE_URL)

    assert body["url"] == REPO
    assert body["note"] == f"Ganymede submission 'Rover' (repo_url), by {member.user.weave_sub}."
    assert body["reference"] == {
        "patch": "ganymede",
        "submission": f"{project.id}/{project.current_revision_id}",
        "project": str(project.id),
        "revision": str(project.current_revision_id),
        "source": "repo_url",
        "project_url": f"{BASE_URL}/projects/{project.id}",
    }
    assert all(isinstance(value, str) for value in body["reference"].values())


# --------------------------------------------------------------------------------------------------
# deliver: one row per response class
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [201, 200])
def test_new_and_duplicate_mark_the_row_archived(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row, status
) -> None:
    row = add_row()
    stash.answers[REPO] = (status, None)

    result = _deliver(db_session, client, weave_stub)

    assert result.archived == 1
    assert (row.state, row.requested_at, row.attempts, row.last_status) == (
        StashArchiveState.ARCHIVED,
        NOW,
        1,
        status,
    )
    assert (row.archive_id, row.page_url, row.archive_status) == ("arc_1", f"{STASH_URL}/archives/1", "queued")
    [request] = stash.posts()
    assert request.headers["authorization"] == f"Bearer {STUB_STASH_TOKEN}"
    assert json.loads(request.content) == archive_request(row, base_url=BASE_URL)


@pytest.mark.parametrize(("status", "error"), [(503, "weave_unavailable"), (500, None), (None, None)])
def test_retryable_answers_keep_the_row_pending_with_backoff(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row, status, error
) -> None:
    row = add_row()
    if status is None:
        stash.network_error_for.add(REPO)
    else:
        stash.answers[REPO] = (status, error)

    result = _deliver(db_session, client, weave_stub)

    assert result.retried == 1
    assert row.state is StashArchiveState.PENDING
    assert (row.attempts, row.first_attempted_at) == (1, NOW)
    assert row.last_status == status
    assert row.next_attempt_at == NOW + timedelta(seconds=30)

    # Not due yet: nothing is sent.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=10))
    assert len(stash.posts()) == 1

    # Due again: the same URL goes again, and the next wait is longer.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=31))
    assert len(stash.posts()) == 2
    assert row.attempts == 2
    assert row.next_attempt_at == NOW + timedelta(seconds=31) + timedelta(seconds=60)


@pytest.mark.parametrize(("status", "error"), [(422, "invalid"), (413, "too_large")])
def test_permanent_refusals_mark_the_row_failed(
    db_session: Session,
    stash: FakeStash,
    client: StashClient,
    weave_stub: StubWeaveClient,
    add_row,
    status,
    error,
    caplog,
) -> None:
    row = add_row()
    stash.answers[REPO] = (status, error)

    with caplog.at_level(logging.WARNING, logger=sender_module.logger.name):
        result = _deliver(db_session, client, weave_stub)

    assert result.failed == 1
    assert (row.state, row.last_status, row.last_error) == (StashArchiveState.FAILED, status, error)
    assert row.next_attempt_at is None
    assert "stash refused to archive" in caplog.text

    _deliver(db_session, client, weave_stub, at=NOW + timedelta(days=1))
    assert len(stash.posts()) == 1


def test_a_429_waits_retry_after_counts_no_attempt_and_stops_the_run(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    first = add_row()
    second = add_row("https://example.com/other")
    stash.answers[REPO] = (429, "rate_limited")
    stash.retry_after = "7"

    result = _deliver(db_session, client, weave_stub)

    assert (result.stopped, result.retried) == ("rate_limited", 1)
    assert len(stash.posts()) == 1
    assert (first.state, first.attempts, first.last_status, first.last_error) == (
        StashArchiveState.PENDING,
        0,
        429,
        "rate_limited",
    )
    assert first.next_attempt_at == NOW + timedelta(seconds=7)
    assert second.attempts == 0

    # Without a usable header, a minute.
    stash.retry_after = "soon"
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=7))
    assert first.next_attempt_at == NOW + timedelta(seconds=7) + timedelta(seconds=60)
    assert first.attempts == 0


def test_a_429_while_reading_archives_leaves_the_rows_for_the_next_run(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row()
    _deliver(db_session, client, weave_stub)
    stash.get_status = 429

    result = _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1))

    assert (result.stopped, result.refreshed) == ("rate_limited", 0)
    db_session.refresh(row)
    assert (row.archive_status, row.last_status, row.updated_at) == ("queued", 201, NOW)
    assert len(stash.gets()) == 1


def test_a_401_retries_later_drops_the_token_and_stops_the_run(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row, caplog
) -> None:
    first = add_row()
    add_row("https://example.com/other")
    stash.answers[REPO] = (401, "invalid_token")

    result = _deliver(db_session, client, weave_stub)

    assert result.stopped == "http_401"
    assert len(stash.posts()) == 1
    assert first.state is StashArchiveState.PENDING
    assert first.next_attempt_at == NOW + timedelta(seconds=30)
    assert weave_stub.stash_token_invalidations == 1
    assert "check KRATER_STASH_URL" in caplog.text


def test_a_403_retries_later_and_keeps_the_token(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row()
    stash.answers[REPO] = (403, "insufficient_scope")

    result = _deliver(db_session, client, weave_stub)

    assert result.stopped == "http_403"
    assert row.state is StashArchiveState.PENDING
    assert weave_stub.stash_token_invalidations == 0


def test_no_token_touches_no_row(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row, monkeypatch
) -> None:
    row = add_row()

    def refuse() -> str:
        raise WeaveUnavailableError("down")

    monkeypatch.setattr(weave_stub, "stash_token", refuse)

    result = _deliver(db_session, client, weave_stub)

    assert result.stopped == "no_token"
    assert stash.requests == []
    assert (row.state, row.attempts) == (StashArchiveState.PENDING, 0)


def test_the_token_is_fetched_once_per_run(
    db_session: Session, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    add_row()
    add_row("https://example.com/other")

    _deliver(db_session, client, weave_stub)

    assert weave_stub.stash_token_requests == 1


def test_a_run_sends_at_most_limit_rows(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    for n in range(3):
        add_row(f"https://example.com/{n}")

    assert _deliver(db_session, client, weave_stub, limit=2).archived == 2
    assert len(stash.posts()) == 2


def test_rows_go_in_the_order_they_were_written(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    # Rows of one transaction share `created_at`, so the order is `seq`, never the random id.
    first = add_row("https://example.com/first")
    second = add_row("https://example.com/second")
    assert first.created_at == second.created_at and first.seq < second.seq

    _deliver(db_session, client, weave_stub, limit=1)

    assert first.state is StashArchiveState.ARCHIVED
    assert second.state is StashArchiveState.PENDING


# --------------------------------------------------------------------------------------------------
# The status refresh
# --------------------------------------------------------------------------------------------------


def test_unfinished_archives_are_refreshed_until_final(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row()
    _deliver(db_session, client, weave_stub)
    assert row.archive_status == "queued"
    # The run that asked for the archive doesn't read it back at once: Stash needs time for the captures.
    assert stash.gets() == []
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(seconds=30))
    assert stash.gets() == []

    stash.archives["arc_1"]["status"] = "running"
    result = _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1))
    assert (result.refreshed, row.archive_status, len(stash.gets())) == (1, "running", 1)

    # Looked at a minute ago: not asked again yet.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1, seconds=30))
    assert len(stash.gets()) == 1

    stash.archives["arc_1"].update(status="done", completed_at="2026-11-08T12:01:12Z")
    result = _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=2))
    assert result.refreshed == 1
    assert (row.archive_status, row.archive_completed_at) == ("done", datetime(2026, 11, 8, 12, 1, 12, tzinfo=UTC))

    # Final: not asked again.
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(days=1))
    assert len(stash.gets()) == 2


def test_the_refresh_copies_stashs_problem_and_clears_it_when_a_rerun_works(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row()
    _deliver(db_session, client, weave_stub)

    stash.archives["arc_1"].update(
        status="partial",
        completed_at="2026-11-08T12:01:12Z",
        problem={
            "code": "not_public",
            "method": "github",
            "message": "NotPublic: GitHub answered 404 for /repos/someone/project: the repository is private or gone.",
            "hint": "The source is private. Ask the owner to share it, then run the archive again.",
        },
    )
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1))

    assert (row.archive_status, row.problem_code) == ("partial", "not_public")
    assert row.problem_hint == "The source is private. Ask the owner to share it, then run the archive again."
    assert row.problem_message is not None and row.problem_message.startswith("NotPublic: GitHub answered 404")
    assert row.submitter_can_fix
    shown = stash_archives.display(row)
    assert (shown.label, shown.tone, shown.problem_code) == ("Link is private", "bad", "not_public")
    assert shown.detail == row.problem_hint

    # Final, so Krater stops polling. An admin reruns the archive in Stash: Krater learns of it at the next
    # submission's archive, not here. But a body without a problem clears the columns.
    row.archive_status = "running"
    # The refresh reads a row again only after a quiet minute. A value equal to the stored one would not be
    # written, and the server would stamp the real time instead.
    row.updated_at = NOW + timedelta(seconds=90)
    db_session.flush()
    stash.archives["arc_1"].update(status="done", problem=None)
    _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=3))

    assert (row.archive_status, row.problem_code, row.problem_hint, row.problem_message) == ("done", None, None, None)
    assert stash_archives.display(row).detail is None


def test_a_deleted_archive_stops_being_polled(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row()
    _deliver(db_session, client, weave_stub)
    del stash.archives["arc_1"]

    _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1))

    assert (row.archive_status, row.last_status) == ("missing", 404)
    assert row.archive_final
    assert stash_archives.display(row).label == "Archive removed"


def test_a_failed_refresh_keeps_the_status_and_tries_next_run(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    row = add_row()
    _deliver(db_session, client, weave_stub)
    stash.network_error_for.add("arc_1")

    result = _deliver(db_session, client, weave_stub, at=NOW + timedelta(minutes=1))

    assert result.refreshed == 0
    assert row.archive_status == "queued"
    assert row.last_status is None and "ConnectError" in (row.last_error or "")
    assert len(stash.gets()) == 1  # one try per run, not a loop


# --------------------------------------------------------------------------------------------------
# run: the worker's entry point
# --------------------------------------------------------------------------------------------------


def test_run_does_nothing_without_a_stash_url(db_session: Session, weave_stub: StubWeaveClient, add_row) -> None:
    row = add_row()

    assert sender_module.run(db_session, weave_stub, Settings(stash_url="")) is None
    assert (row.state, weave_stub.stash_token_requests) == (StashArchiveState.PENDING, 0)


def test_rows_of_two_projects_are_independent(
    db_session: Session, stash: FakeStash, client: StashClient, weave_stub: StubWeaveClient, add_row
) -> None:
    bad = add_row("https://example.com/bad")
    good = add_row("https://example.com/good")
    stash.answers[bad.url] = (422, "invalid")

    result = _deliver(db_session, client, weave_stub)

    assert (result.failed, result.archived) == (1, 1)
    assert (bad.state, good.state) == (StashArchiveState.FAILED, StashArchiveState.ARCHIVED)
