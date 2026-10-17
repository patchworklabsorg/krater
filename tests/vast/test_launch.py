"""Vast's launch modes for Krater's neutral `Launch`."""

from __future__ import annotations

from decimal import Decimal

from krater.providers import Launch
from krater.vast.launch import create_request


def _launch(**fields) -> Launch:
    return Launch(image="ubuntu:24.04", disk_gb=16, label="krater-job-x-1", bootstrap="echo hi", **fields)


def test_a_job_replaces_the_entrypoint_with_sh_in_args_mode() -> None:
    request = create_request(_launch(bid_price=Decimal("0.055"), env={"KRATER_JOB_TOKEN": "secret"}))
    assert (request.runtype, request.onstart, request.args) == ("args", "sh", ("-c", "echo hi"))
    assert request.bid_price == Decimal("0.055") and request.env == {"KRATER_JOB_TOKEN": "secret"}


def test_a_box_runs_the_bootstrap_as_vasts_ssh_startup_script() -> None:
    request = create_request(_launch(box=True))
    assert (request.runtype, request.onstart, request.args) == ("ssh_direct", "echo hi", None)
    assert request.bid_price is None


def test_a_launch_never_shows_its_environment() -> None:
    assert "secret" not in repr(_launch(env={"KRATER_JOB_TOKEN": "secret"}))
