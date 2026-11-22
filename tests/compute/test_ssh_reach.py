"""A box's SSH address: the direct port while it takes connections, Vast's proxy when the host refuses them."""

from __future__ import annotations

import socket
from datetime import timedelta

from krater.compute import runner_api, scheduler, ssh_reach
from krater.config import get_settings
from tests.compute.conftest import current_weave
from tests.compute.test_boxes import _box, _events
from tests.compute.test_scheduler import NOW, RUNNER_SHA, SETTINGS, _instance, _pass, _submit, _token

DIRECT = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41022"}]}


class _Check:
    """Answers `answer`, and records each address asked about."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, int]] = []

    def direct_works(self, instance_id, host, port, *, now):
        self.asked.append((instance_id, host, port))
        return self.answer


def _checked_pass(db_session, client, check, minute: int) -> None:
    scheduler.run_pass(
        db_session,
        client,
        SETTINGS,
        now=NOW + timedelta(minutes=minute),
        runner_sha256=RUNNER_SHA,
        weave_client=current_weave[0],
        ssh_check=check,
    )


def _running(db_session, client, fake, job):
    """`job`'s machine running with a direct port, its runner started and reporting (minute 2)."""
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running", public_ipaddr="198.51.100.7", ports=DIRECT)
    instance = runner_api.authenticate(db_session, _token(fake, row))
    runner_api.start(db_session, instance, now=NOW + timedelta(minutes=1), settings=get_settings())
    _pass(db_session, client, NOW + timedelta(minutes=2))
    return row


def _beat(db_session, row, minute: int) -> None:
    runner_api.heartbeat(db_session, row, {"gpu_util": 50, "sessions": 1}, now=NOW + timedelta(minutes=minute))


def _proxy(fake, row) -> tuple[str, int]:
    seen = fake.instances[row.provider_instance_id]
    return seen["ssh_host"], seen["ssh_port"]


def test_a_refused_direct_port_falls_back_to_the_proxy_once(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)
    check = _Check(False)

    for minute in (3, 4):
        _beat(db_session, row, minute)
        _checked_pass(db_session, client, check, minute)

    assert (row.ssh_host, row.ssh_port) == _proxy(fake, row)
    assert check.asked[0][1:] == ("198.51.100.7", 41022)
    assert _events(db_session, job).count("ssh_proxy") == 1


def test_a_working_direct_port_is_kept(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)

    _beat(db_session, row, 3)
    _checked_pass(db_session, client, _Check(True), 3)

    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)
    assert "ssh_proxy" not in _events(db_session, job)


def test_no_answer_decides_nothing(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)

    _beat(db_session, row, 3)
    _checked_pass(db_session, client, _Check(None), 3)

    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)
    assert "ssh_proxy" not in _events(db_session, job)


def test_after_the_fallback_an_untested_pass_keeps_the_proxy(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)
    _beat(db_session, row, 3)
    _checked_pass(db_session, client, _Check(False), 3)

    # The runner goes quiet: the port can't be tested now, and the dead direct address must not come back.
    check = _Check(True)
    _checked_pass(db_session, client, check, 12)

    assert check.asked == []
    assert (row.ssh_host, row.ssh_port) == _proxy(fake, row)


def test_a_direct_port_that_works_again_is_used_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)
    _beat(db_session, row, 3)
    _checked_pass(db_session, client, _Check(False), 3)

    _beat(db_session, row, 4)
    _checked_pass(db_session, client, _Check(True), 4)
    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)

    _beat(db_session, row, 5)
    _checked_pass(db_session, client, _Check(False), 5)
    assert (row.ssh_host, row.ssh_port) == _proxy(fake, row)
    assert _events(db_session, job).count("ssh_proxy") == 1


def test_the_port_isnt_tested_before_the_runner_starts(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running", public_ipaddr="198.51.100.7", ports=DIRECT)
    check = _Check(False)

    _checked_pass(db_session, client, check, 1)

    assert check.asked == []
    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)


def test_a_batch_jobs_ports_arent_tested(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    row = _running(db_session, client, fake, job)
    check = _Check(False)

    _beat(db_session, row, 3)
    _checked_pass(db_session, client, check, 3)

    assert check.asked == []


# -- The live check ------------------------------------------------------------------------------------


def _live(answers):
    probed: list[tuple[str, int]] = []

    def probe(host, port):
        probed.append((host, port))
        return answers.pop(0)

    return ssh_reach.LiveSshCheck(probe=probe), probed


HOST = "38.114.93.198"


def test_a_working_port_is_tested_once() -> None:
    check, probed = _live([True])

    assert check.direct_works("1", HOST, 20056, now=NOW) is True
    assert check.direct_works("1", HOST, 20056, now=NOW + timedelta(days=1)) is True
    assert probed == [(HOST, 20056)]


def test_a_refused_port_is_tested_again_after_the_recheck_time() -> None:
    check, probed = _live([False, True])

    assert check.direct_works("1", HOST, 20056, now=NOW) is False
    assert check.direct_works("1", HOST, 20056, now=NOW + timedelta(minutes=5)) is False
    assert check.direct_works("1", HOST, 20056, now=NOW + ssh_reach.RECHECK) is True
    assert len(probed) == 2


def test_a_pass_tests_only_a_few_new_addresses() -> None:
    check, probed = _live([False] * 10)

    answers = [check.direct_works(str(i), HOST, 20000 + i, now=NOW) for i in range(ssh_reach.PROBES_PER_MINUTE + 2)]

    assert answers[-2:] == [None, None]
    assert len(probed) == ssh_reach.PROBES_PER_MINUTE
    assert check.direct_works("99", HOST, 20099, now=NOW + timedelta(minutes=1)) is False


def test_a_private_address_or_a_bad_port_is_never_connected_to() -> None:
    check, probed = _live([])

    assert check.direct_works("1", "127.0.0.1", 22, now=NOW) is False
    assert check.direct_works("1", "10.0.0.5", 22, now=NOW) is False
    assert check.direct_works("1", "169.254.169.254", 80, now=NOW) is False
    assert check.direct_works("1", HOST, 70000, now=NOW) is False
    assert check.direct_works("1", "not-an-ip", 22, now=NOW) is False
    assert probed == []


def test_the_probe_tells_open_from_refused() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert ssh_reach.probe_tcp("127.0.0.1", port) is True
    assert ssh_reach.probe_tcp("127.0.0.1", port) is False
    assert ssh_reach.probe_tcp("127.0.0.1", 70000) is None


def test_a_paused_box_whose_runner_hasnt_started_again_isnt_tested(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)
    _beat(db_session, row, 3)
    row.runner_starts_needed = row.runner_starts + 1  # what a pause sets: the container restarts on resume
    check = _Check(False)

    _checked_pass(db_session, client, check, 3)

    assert check.asked == []
    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)


def test_a_quiet_runner_means_no_test(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    _running(db_session, client, fake, job)
    check = _Check(False)

    _checked_pass(db_session, client, check, 3 + 10)

    assert check.asked == []


def test_no_test_while_krater_s_network_is_down(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)
    _beat(db_session, row, 3)
    check = _Check(False)

    class _Down:
        def problem(self, session, *, now):
            return "the edge is down"

    scheduler.run_pass(
        db_session,
        client,
        SETTINGS,
        now=NOW + timedelta(minutes=3),
        runner_sha256=RUNNER_SHA,
        weave_client=current_weave[0],
        ssh_check=check,
        network=_Down(),
    )

    assert check.asked == []


def test_a_proxy_that_changes_host_keeps_the_box_off_the_dead_port(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _running(db_session, client, fake, job)
    _beat(db_session, row, 3)
    _checked_pass(db_session, client, _Check(False), 3)
    fake.instances[row.provider_instance_id]["ssh_host"] = "ssh7.vast.ai"

    _checked_pass(db_session, client, _Check(True), 12)  # quiet runner: untested

    assert row.ssh_host == "ssh7.vast.ai"
