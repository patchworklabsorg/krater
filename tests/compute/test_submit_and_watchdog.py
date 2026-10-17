"""The staging helpers: `python -m krater.compute.submit` and scripts/dev/compute_watchdog.py."""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from krater.compute import submit
from krater.config import Settings
from krater.vast import CreateInstance
from tests.compute.test_scheduler import NOW, SETTINGS, _pass


def _watchdog():
    path = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "compute_watchdog.py"
    spec = importlib.util.spec_from_file_location("compute_watchdog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_submit_list_and_cancel(db_session, project, client, fake) -> None:  # noqa: F811
    out = submit.run(
        [
            "submit",
            "--project",
            str(project.id),
            "--name",
            "smoke",
            "--image",
            "alpine:3.20",
            "--command",
            "echo hi",
            "--gpu",
            "RTX 3060",
            "--disk",
            "8",
            "--target",
            "0.08",
            "--max",
            "0.12",
            "--max-runtime",
            "20",
        ],
        db_session,
        Settings(),
        now=NOW,
    )
    assert out.startswith("Queued ")
    job_id = out.split()[1].rstrip(".")

    _pass(db_session, client, NOW + timedelta(seconds=5))
    assert "starting" in submit.run(["list", "--project", str(project.id)], db_session, SETTINGS, now=NOW)

    assert submit.run(["cancel", "--job", job_id], db_session, SETTINGS, now=NOW).startswith("Cancelled")
    _pass(db_session, client, NOW + timedelta(minutes=1))
    assert fake.instances == {}


def test_submit_refuses_a_max_over_the_cap(db_session, project) -> None:  # noqa: F811
    from krater.services.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        submit.run(
            [
                "submit",
                "--project",
                str(project.id),
                "--name",
                "x",
                "--image",
                "alpine:3.20",
                "--command",
                "true",
                "--gpu",
                "RTX 3060",
                "--target",
                "1",
                "--max",
                "9",
            ],
            db_session,
            Settings(),
            now=NOW,
        )


def test_the_watchdog_destroys_only_its_prefix_once_over_the_cap(client, fake) -> None:  # noqa: F811
    watchdog = _watchdog()
    mine = client.create_instance(1001, CreateInstance(image="x", disk_gb=8, label="krater-staging-abc-1"))
    other = client.create_instance(1003, CreateInstance(image="x", disk_gb=8, label="krater-spike-1"))
    fake.add_charge(mine, gpu="0.20", disk="0.06")
    watchdog.time.sleep = lambda seconds: None

    code = watchdog.watch(
        client, prefix="krater-staging-", cap=Decimal("0.25"), deadline=timedelta(hours=1), every=0, once=True
    )

    assert code == 0
    assert mine not in fake.instances and other in fake.instances


def test_the_watchdog_keeps_destroying_through_vast_errors(client, fake) -> None:  # noqa: F811
    watchdog = _watchdog()
    mine = client.create_instance(1001, CreateInstance(image="x", disk_gb=8, label="krater-staging-abc-1"))
    watchdog.time.sleep = lambda seconds: None
    fake.fail_next(500, times=12)  # the check, and the first destroy rounds

    code = watchdog.watch(client, prefix="krater-staging-", cap=Decimal(1), deadline=timedelta(0), every=0, once=True)

    assert code == 0
    assert mine not in fake.instances


def test_the_watchdog_refuses_a_broad_prefix(client) -> None:  # noqa: F811
    with pytest.raises(SystemExit):
        _watchdog().watch(client, prefix="krater-", cap=Decimal(1), deadline=timedelta(hours=1), every=0, once=True)


def test_nothing_happens_under_the_cap(client, fake) -> None:  # noqa: F811
    watchdog = _watchdog()
    mine = client.create_instance(1001, CreateInstance(image="x", disk_gb=8, label="krater-staging-abc-1"))

    assert (
        watchdog.watch(
            client, prefix="krater-staging-", cap=Decimal("0.25"), deadline=timedelta(hours=1), every=0, once=True
        )
        == 0
    )
    assert mine in fake.instances
