"""`krater.services.network_watch`: alastor's ping and the outbound test, which hold actions on silent runners."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from krater.services import network_watch

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def test_the_ping_is_written_at_most_every_thirty_seconds(db_session: Session) -> None:
    assert network_watch.record_edge_ping(db_session, now=NOW)
    assert not network_watch.record_edge_ping(db_session, now=NOW + timedelta(seconds=10))
    assert network_watch.last_edge_ping(db_session) == NOW

    assert network_watch.record_edge_ping(db_session, now=NOW + network_watch.EDGE_PING_WRITE_EVERY)
    assert network_watch.last_edge_ping(db_session) == NOW + network_watch.EDGE_PING_WRITE_EVERY


def test_inbound_is_down_only_once_a_ping_seen_before_goes_stale(db_session: Session) -> None:
    assert network_watch.inbound_problem(db_session, now=NOW) is None  # never set up: no hold

    network_watch.record_edge_ping(db_session, now=NOW)
    assert network_watch.inbound_problem(db_session, now=NOW + timedelta(minutes=3)) is None
    problem = network_watch.inbound_problem(db_session, now=NOW + network_watch.EDGE_PING_FRESH)
    assert problem is not None and "2026-11-15 12:00 UTC" in problem


def test_the_outbound_test_counts_only_once_it_has_passed_here(db_session: Session) -> None:
    results = iter(["no connection to 1.1.1.1:443 (timed out)", None, "no connection to 1.1.1.1:443 (timed out)"])
    check = network_watch.LiveNetworkCheck(probe=lambda: next(results))

    assert check.problem(db_session, now=NOW) is None  # never passed here: a firewall, not an outage
    assert check.problem(db_session, now=NOW) is None  # passes
    problem = check.problem(db_session, now=NOW)
    assert problem is not None and "own internet connection is down" in problem and "1.1.1.1" in problem


def test_a_stale_ping_is_a_problem_without_running_the_outbound_test(db_session: Session) -> None:
    calls = []
    check = network_watch.LiveNetworkCheck(probe=lambda: calls.append(1))
    network_watch.record_edge_ping(db_session, now=NOW)

    assert "alastor's ping" in check.problem(db_session, now=NOW + timedelta(minutes=10))
    assert calls == []


def test_the_real_outbound_test_runs() -> None:
    """Either answer is fine here (CI may have no internet); it must return, not raise."""
    result = network_watch.probe_outbound()
    assert result is None or result.startswith(("no connection", "connections work"))
