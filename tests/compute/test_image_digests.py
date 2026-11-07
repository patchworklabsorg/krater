"""The image digest Krater records when it rents a machine, and the pinned launch (compute_pin_image_digest)."""

from __future__ import annotations

from krater.compute import scheduler
from krater.config import Settings
from krater.registry import FakeRegistry, ImageDetails
from tests.compute.conftest import current_weave
from tests.compute.test_scheduler import NOW, RUNNER_SHA, _instance, _submit

DIGEST = "sha256:" + "d" * 64
IMAGE = "ghcr.io/alice/train:v3"


def _registry() -> FakeRegistry:
    registry = FakeRegistry()
    registry.add(
        IMAGE,
        ImageDetails(digest=DIGEST, platform_digest=None, platform="linux/amd64", created=None, size_bytes=0, layers=0),
    )
    return registry


def _pass(db_session, client, registry, *, pin: bool):
    settings = Settings(compute_mode="vast", compute_max_hourly_cost_cents=500, compute_pin_image_digest=pin)
    return scheduler.run_pass(
        db_session,
        client,
        settings,
        now=NOW,
        runner_sha256=RUNNER_SHA,
        weave_client=current_weave[0],
        registry=registry,
    )


def test_a_rental_records_the_digest_and_launches_the_tag_by_default(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, image=IMAGE)

    _pass(db_session, client, _registry(), pin=False)

    row = _instance(db_session, job)
    assert row.image_digest == DIGEST
    assert row.image_pinned is False
    assert fake.instances[row.provider_instance_id]["image"] == IMAGE


def test_with_pinning_on_the_machine_gets_exactly_that_digest(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, image=IMAGE)

    _pass(db_session, client, _registry(), pin=True)

    row = _instance(db_session, job)
    assert row.image_pinned is True
    assert fake.instances[row.provider_instance_id]["image"] == f"ghcr.io/alice/train@{DIGEST}"


def test_a_registry_that_cant_answer_doesnt_stop_the_rental(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, image=IMAGE)
    registry = _registry()
    registry.fail = "ghcr.io is down"

    _pass(db_session, client, registry, pin=True)

    row = _instance(db_session, job)
    assert row.image_digest is None
    assert row.image_pinned is False
    assert fake.instances[row.provider_instance_id]["image"] == IMAGE


def test_one_lookup_per_image_per_pass(db_session, member, project, client, fake) -> None:  # noqa: F811
    _submit(db_session, member, project, image=IMAGE, name="a")
    _submit(db_session, member, project, image=IMAGE, name="b")
    registry = _registry()

    _pass(db_session, client, registry, pin=False)

    assert registry.calls.count(("resolve", IMAGE)) == 1
