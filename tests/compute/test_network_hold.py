"""The scheduler holds actions on a silent runner while Krater's own network is down (`krater.services.network_watch`):
runners report over Cloudflare, alastor and the tailnet, so when that path or the server's connection is down every
runner looks dead and none is."""

from __future__ import annotations

from datetime import timedelta

from krater.compute import scheduler
from krater.models import ComputeInstanceState, ComputeJobState
from krater.services import network_watch
from tests.compute.conftest import current_weave
from tests.compute.test_boxes import _beat, _events, _running_box
from tests.compute.test_scheduler import NOW, RUNNER_SHA, SETTINGS


class _Network:
    """A stand-in for the worker's `LiveNetworkCheck`: down while `problem_text` is set."""

    def __init__(self) -> None:
        self.problem_text: str | None = None
        self.asked = 0

    def problem(self, session, *, now):
        self.asked += 1
        return self.problem_text


def _pass(db_session, client, network, minute):
    return scheduler.run_pass(
        db_session,
        client,
        SETTINGS,
        now=NOW + timedelta(minutes=minute),
        runner_sha256=RUNNER_SHA,
        weave_client=current_weave[0],
        network=network,
    )


def test_a_silent_runner_is_left_alone_while_the_network_is_down(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3)
    network = _Network()
    network.problem_text = "alastor's ping hasn't reached Krater since 12:03 UTC"

    for minute in range(4, 60):  # well past the 30-minute heartbeat timeout
        _pass(db_session, client, network, minute)

    assert job.state is ComputeJobState.RUNNING and row.state is ComputeInstanceState.RUNNING
    assert fake.instances[row.provider_instance_id]["intended_status"] == "running"
    assert _events(db_session, job).count("network_hold") == 1
    assert network.asked > 0

    # Back at minute 60: the runner gets its whole timeout again, from the last held pass, before Krater acts.
    network.problem_text = None
    for minute in range(60, 89):
        _pass(db_session, client, network, minute)
    assert job.state is ComputeJobState.RUNNING, (job.state_reason, _events(db_session, job))
    for minute in range(89, 92):
        _pass(db_session, client, network, minute)
    assert job.state is ComputeJobState.STOPPED and "stopped reporting" in job.state_reason


def test_a_runner_that_reports_again_after_the_hold_carries_on(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3)
    network = _Network()
    network.problem_text = "the server's own internet connection is down"
    for minute in range(4, 50):
        _pass(db_session, client, network, minute)

    network.problem_text = None
    for minute in range(50, 120):
        _beat(db_session, row, minute - 0.5, gpu_util=99)  # busy, so it isn't stopped as idle
        _pass(db_session, client, network, minute)

    assert job.state is ComputeJobState.RUNNING


def test_a_hold_ends_after_its_limit(db_session, member, project, client, fake) -> None:  # noqa: F811
    """A runner that really died mustn't bill without end behind a broken check: after `HOLD_LIMIT` of silence it
    gets one last heartbeat timeout, then Krater acts."""
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3)
    network = _Network()
    network.problem_text = "alastor's ping hasn't reached Krater"
    limit = int(network_watch.HOLD_LIMIT.total_seconds() // 60)

    for minute in range(4, limit + 3, 5):
        _pass(db_session, client, network, minute)
    assert job.state is ComputeJobState.RUNNING
    for minute in range(limit + 3, limit + 40):
        _pass(db_session, client, network, minute)

    assert job.state is ComputeJobState.STOPPED and "stopped reporting" in job.state_reason


def test_a_reporting_runner_never_asks_about_the_network(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_box(db_session, member, project, client, fake)
    network = _Network()
    for minute in range(3, 40):
        _beat(db_session, row, minute - 0.5, gpu_util=99)
        _pass(db_session, client, network, minute)

    assert job.state is ComputeJobState.RUNNING and network.asked == 0


def test_a_silent_box_isnt_judged_idle_from_its_last_report(db_session, member, project, client, fake) -> None:  # noqa: F811
    """A runner's last heartbeat said "idle"; then it went silent. What it said is stale: its silence is for the
    heartbeat timeout (which knows about the network) to judge, not the idle stop."""
    job, row = _running_box(db_session, member, project, client, fake)
    _beat(db_session, row, 3, gpu_util=0, sessions=0)

    for minute in range(4, 60):
        _pass(db_session, client, None, minute)

    assert "stop_requested" not in _events(db_session, job)
    assert job.state is ComputeJobState.STOPPED and "stopped reporting" in job.state_reason
