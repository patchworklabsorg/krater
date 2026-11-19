"""A job's own checkpoint upload interval (`checkpoint_upload_minutes`): the runner is told it at start, a box keeps
the default, and the rules on what it can be."""

from __future__ import annotations

import pytest

from krater.compute import runner_api
from krater.config import get_settings
from krater.models import ComputeJobKind
from krater.services import compute_jobs
from krater.services.errors import ValidationFailed
from tests.compute.test_scheduler import NOW, _running_job, _submit, _token


def _start(db_session, fake, row):
    return runner_api.start(
        db_session, runner_api.authenticate(db_session, _token(fake, row)), now=NOW, settings=get_settings()
    )


def test_the_default_is_the_runners_timer() -> None:
    assert compute_jobs.DEFAULT_CHECKPOINT_UPLOAD_MINUTES * 60 == runner_api.CHECKPOINT_SYNC_SECONDS


def test_the_runner_is_told_the_jobs_interval(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, checkpoint_upload_minutes=2)

    assert job.checkpoint_upload_minutes == 2
    assert _start(db_session, fake, row).checkpoint_sync_seconds == 120


def test_a_job_without_one_uploads_on_the_default_timer(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)

    assert job.checkpoint_upload_minutes is None
    assert _start(db_session, fake, row).checkpoint_sync_seconds == runner_api.CHECKPOINT_SYNC_SECONDS


def test_a_box_keeps_the_default_whatever_its_row_says(db_session, member, project) -> None:  # noqa: F811
    # The same timer saves a box's /workspace: a row edited by hand mustn't change it.
    job = _submit(db_session, member, project)
    job.kind = ComputeJobKind.BOX
    job.checkpoint_upload_minutes = 1
    assert runner_api.checkpoint_sync_seconds(job) == runner_api.CHECKPOINT_SYNC_SECONDS


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kind": ComputeJobKind.BOX, "command": None}, "A box saves /workspace every 10 minutes"),
        ({"checkpoint_upload_minutes": 0}, "Choose a checkpoint upload every 1 to 60 minutes"),
        ({"checkpoint_upload_minutes": 61}, "Choose a checkpoint upload every 1 to 60 minutes"),
    ],
)
def test_what_cant_be_an_upload_interval(db_session, member, project, overrides, message) -> None:  # noqa: F811
    overrides = {"checkpoint_upload_minutes": 5, **overrides}
    with pytest.raises(ValidationFailed) as refused:
        _submit(db_session, member, project, **overrides)
    assert refused.value.errors["checkpoint_upload_minutes"].startswith(message)
