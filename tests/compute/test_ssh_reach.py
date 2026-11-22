"""A box's SSH address: the direct port while it takes connections, Vast's proxy when it refuses them."""

from __future__ import annotations

from datetime import timedelta

from krater.compute import runner_api, scheduler, ssh_reach
from krater.config import get_settings
from tests.compute.conftest import current_weave
from tests.compute.test_boxes import _box, _events
from tests.compute.test_scheduler import NOW, RUNNER_SHA, SETTINGS, _instance, _pass, _submit, _token

DIRECT = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41022"}]}


class _Check:
    """Answers from `works`, and records each address asked about."""

    def __init__(self, works: bool) -> None:
        self.works = works
        self.asked: list[tuple[str, str, int]] = []

    def direct_works(self, instance_id: str, host: str, port: int) -> bool:
        self.asked.append((instance_id, host, port))
        return self.works


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


def _rented(db_session, client, fake, job):
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.provider_instance_id, "running", public_ipaddr="198.51.100.7", ports=DIRECT)
    return row


def _runner_up(db_session, fake, row) -> None:
    instance = runner_api.authenticate(db_session, _token(fake, row))
    runner_api.start(db_session, instance, now=NOW + timedelta(minutes=1), settings=get_settings())


def test_a_refused_direct_port_falls_back_to_the_proxy(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _rented(db_session, client, fake, job)
    _runner_up(db_session, fake, row)
    proxy = (fake.instances[row.provider_instance_id]["ssh_host"], fake.instances[row.provider_instance_id]["ssh_port"])
    check = _Check(works=False)

    _checked_pass(db_session, client, check, 2)
    _checked_pass(db_session, client, check, 3)

    assert (row.ssh_host, row.ssh_port) == proxy
    assert check.asked[0][1:] == ("198.51.100.7", 41022)
    assert _events(db_session, job).count("ssh_proxy") == 1


def test_a_working_direct_port_is_kept(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _rented(db_session, client, fake, job)
    _runner_up(db_session, fake, row)

    _checked_pass(db_session, client, _Check(works=True), 2)

    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)
    assert "ssh_proxy" not in _events(db_session, job)


def test_the_direct_port_isnt_tested_before_the_runner_starts(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _box(db_session, member, project)
    row = _rented(db_session, client, fake, job)
    check = _Check(works=False)

    _checked_pass(db_session, client, check, 1)

    assert check.asked == []
    assert (row.ssh_host, row.ssh_port) == ("198.51.100.7", 41022)


def test_a_batch_jobs_ports_arent_tested(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    row = _rented(db_session, client, fake, job)
    _runner_up(db_session, fake, row)
    check = _Check(works=False)

    _checked_pass(db_session, client, check, 2)

    assert check.asked == []


def test_the_live_check_tests_each_address_once() -> None:
    probed: list[tuple[str, int]] = []

    def probe(host: str, port: int) -> bool:
        probed.append((host, port))
        return False

    check = ssh_reach.LiveSshCheck(probe=probe)

    assert check.direct_works("1", "198.51.100.7", 41022) is False
    assert check.direct_works("1", "198.51.100.7", 41022) is False
    assert check.direct_works("1", "198.51.100.7", 41023) is False
    assert probed == [("198.51.100.7", 41022), ("198.51.100.7", 41023)]


def test_the_probe_says_whether_a_port_takes_connections() -> None:
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert ssh_reach.probe_tcp("127.0.0.1", port) is True
    assert ssh_reach.probe_tcp("127.0.0.1", port) is False
